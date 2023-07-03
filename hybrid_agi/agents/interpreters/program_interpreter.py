"""The graph program interpreter. Copyright (C) 2023 SynaLinks. License: GPL-3.0"""
from collections import deque
from typing import List, Optional, Iterable
from pydantic import BaseModel, Extra
from colorama import Fore, Style
from redisgraph import Node, Graph 
from collections import OrderedDict

from langchain.chains.llm import LLMChain
from langchain.tools import Tool
from langchain.base_language import BaseLanguageModel
from hybrid_agi.hybridstores.redisgraph import RedisGraphHybridStore
from langchain.prompts.prompt import PromptTemplate

class GraphProgramInterpreter(BaseModel):
    """LLM based interpreter for graph programs"""
    hybridstore: RedisGraphHybridStore
    llm: BaseLanguageModel
    program_key: str
    prompt: str = ""
    default_prompt: str = ""
    final_prompt: str = ""
    monitoring_prompt: str = ""
    program_stack: Iterable = deque()
    max_iteration: int = 50
    max_decision_attemps: int = 5
    allowed_tools: List[str] = []
    tools_map: OrderedDict[str, Tool] = {}
    language: str = "English"
    tools_instructions: str = ""
    monitoring: bool = True
    verbose: bool = True

    class Config:
        """Configuration for this pydantic object."""
        extra = Extra.forbid
        arbitrary_types_allowed = True

    def __init__(
            self,
            hybridstore: RedisGraphHybridStore,
            llm: BaseLanguageModel,
            program_key:str = "",
            prompt:str = "",
            monitoring_prompt:str = "",
            final_prompt:str = "",
            tools:List[Tool] = [],
            max_iteration: int = 50,
            max_decision_attemps: int = 5,
            language: str = "English",
            monitoring: bool = True,
            verbose: bool = True
        ):
        if program_key == "":
            program_key = hybridstore.main.name
        final_prompt = final_prompt if final_prompt else "Final Answer:"
        monitoring_prompt = monitoring_prompt if monitoring_prompt else "Critisize and show your work. Without additionnal information.\nCritique:"
        super().__init__(
            hybridstore = hybridstore,
            llm = llm,
            program_key = program_key,
            program = program,
            prompt = prompt,
            monitoring_prompt = monitoring_prompt,
            final_prompt = final_prompt,
            default_prompt = prompt,
            max_iteration = max_iteration,
            max_decision_attemps = max_decision_attemps,
            language = language,
            monitoring = monitoring,
            verbose = verbose
        )
        self.tools_instructions = "You have access to the following tools:\n"
        for tool in tools:
            self.tools_instructions += f"{tool.name}:{tool.description}"
            self.allowed_tools.append(tool.name)
            self.tools_map[tool.name] = tool
        self.prompt += self.tools_instructions

    def get_current_program(self) -> Optional[Graph]:
        """Method to retreive the current plan from the stack"""
        if len(self.program_stack) > 0:
            return self.program_stack[len(self.program_stack)-1]
        return None

    def get_next(self, node:Node) -> Optional[Node]:
        """Method to get the next node"""
        name = node.properties["name"]
        result = self.get_current_program().query('MATCH ({name:"'+name+'"})-[:NEXT]->(n) RETURN n')
        if len(result.result_set) > 0:
            return result.result_set[0][0]
        return None

    def predict(self, prompt:str) -> str:
        """Predict the next words"""
        prompt_template = PromptTemplate.from_template(self.prompt+"\n"+prompt)
        chain = LLMChain(llm=self.llm, prompt=prompt_template, verbose=False)
        prediction = chain.predict(language=self.language)
        return prediction

    def execute_program(self, program_index:str):
        """Method to execute a program"""
        program = Graph(program_index, self.hybridstore.client)
        self.program_stack.append(program)
        result = self.get_current_program().query('MATCH (n:Control {name:"Start"}) RETURN n')
        if len(result.result_set) == 0:
            raise ValueError("No entry point in the program")
        if len(result.result_set) > 1:
            raise ValueError("Multiple entry point in the program")
        starting_node = result.result_set[0][0]
        current_node = self.get_next(starting_node)
        next_node = None
        iteration = 0
        while True:
            if current_node.label == "Program":
                program_index = current_node.properties["name"]
                self.execute_program(program_index)
                next_node = self.get_next(current_node)
            elif current_node.label == "Action":
                self.use_tool(current_node)
                next_node = self.get_next(current_node)
                if self.monitoring:
                    self.monitor()
            elif current_node.label == "Decision":
                next_node = self.decide(current_node)
                if self.monitoring:
                    self.monitor()
            elif current_node.label == "Control":
                if current_node.properties["name"] == "End":
                    break
            else:
                raise RuntimeError("Invalid label for node. Please verify your programs.")
            if next_node is None:
                raise RuntimeError("Program failed after reaching a non-terminated path. Please verify your programs.")
            current_node = next_node
            iteration += 1
            if iteration > self.max_iteration:
                raise RuntimeError("Program failed after reaching max iteration")
        self.program_stack.pop()

    def decide(self, node:Node) -> Node:
        """Method to make a decision"""
        # get possible output options
        question = node.properties["name"]
        purpose = node.properties["purpose"]
        prompt = f"Decision: {question} Please answer without additional information.\nDecision Purpose: {purpose}\n"
        outcomes = []
        result = self.get_current_program().query('MATCH (n:Decision {name:"'+question+'", purpose:"'+purpose+'"})-[r]->() RETURN type(r)')
        for record in result.result_set:
            outcomes.append(record[0])
        choice = " or ".join(outcomes)
        prompt += f"Decision Answer (must be {choice}): "
        decision = ""
        attemps = 0
        while True:
            decision = self.predict(prompt).strip()
            attemps += 1
            if decision in outcomes:
                break
            if attemps > self.max_decision_attemps:
                raise ValueError(f"Failed to decide after {attemps} attemps. Please verify your programs.")
        prompt += decision
        self.update(prompt)
        result = self.get_current_program().query('MATCH (:Decision {name:"'+question+'", purpose:"'+purpose+'"})-[:'+decision+']->(n) RETURN n')
        next_node = result.result_set[0][0]
        return next_node

    def use_tool(self, node:Node) -> str:
        """Method to use a tool"""
        action_purpose = node.properties["name"]
        tool_name = node.properties["tool"]
        tool_params_prompt = node.properties["params"]
        tool_prompt = f"Action: {tool_name}\nAction Purpose: {action_purpose}\nAction Input: {tool_params_prompt}"
        if tool_name != "Predict":
            tool_input = self.predict(tool_prompt)
            observation = self.execute_tool(tool_name, tool_input)
            final_prompt = f"Action: {tool_name}\nAction Purpose: {action_purpose}\nAction Input: {tool_input}\nAction Observation: {observation}"
        else:
            tool_input = tool_prompt
            observation = self.predict(tool_prompt)
            final_prompt = f"Action: {tool_name}\nAction Purpose: {action_purpose}\nAction Input: {tool_input}\n{observation}"
        self.update(final_prompt)

    def execute_tool(self, name:str, query:str):
        """Method to run the given tool"""
        if name not in self.allowed_tools:
            raise ValueError(f"Tool '{name}' not allowed. Please use another one.")
        if name not in self.tools_map:
            (f"Tool '{name}' not registered. Please use another one.")
        try:
            return self.tools_map[name].run(query)
        except Exception as err:
            return str(err)

    def update(self, prompt:str):
        """Method to update the program prompt"""
        if self.verbose:
            if prompt.startswith("Action:"):
                print(f"{Fore.CYAN}{prompt}{Style.RESET_ALL}")
            elif prompt.startswith("Decision:"):
                print(f"{Fore.BLUE}{prompt}{Style.RESET_ALL}")
            elif prompt.startswith("Critique:"):
                print(f"{Fore.MAGENTA}{prompt}{Style.RESET_ALL}")
            else:
                print(f"{Fore.GREEN}{prompt}{Style.RESET_ALL}")
        self.prompt += "\n" + prompt

    def monitor(self):
        """Method to monitor the process"""
        critique = self.predict(self.monitoring_prompt)
        self.update(f"Critique: {critique}")

    def clear(self):
        """Method to clear the prompt"""
        self.prompt = self.default_prompt + self.tools_instructions

    def run(self, objective:str):
        """Method to run the agent"""
        self.clear()
        self.update(f"The objective given by the User is: {objective}")
        try:
            self.execute_program(self.program_key)
            result = self.predict(self.final_prompt)
            return result
        except Exception as err:
            return f"Error occured: {str(err)}"