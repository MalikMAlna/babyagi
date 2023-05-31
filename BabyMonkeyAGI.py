#!/usr/bin/env python3
import os
import subprocess
import time
from collections import deque
from typing import Dict, List
import importlib
import re
import openai
import pinecone
from dotenv import load_dotenv

# Load default environment variables (.env)
load_dotenv()

# Engine configuration

# API Keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
assert OPENAI_API_KEY, "OPENAI_API_KEY environment variable is missing from .env"

OPENAI_API_MODEL = os.getenv("OPENAI_API_MODEL", "gpt-3.5-turbo")

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY", "")
assert PINECONE_API_KEY, "PINECONE_API_KEY environment variable is missing from .env"

PINECONE_ENVIRONMENT = os.getenv("PINECONE_ENVIRONMENT", "")
assert (
    PINECONE_ENVIRONMENT
), "PINECONE_ENVIRONMENT environment variable is missing from .env"

# Table config
YOUR_TABLE_NAME = os.getenv("TABLE_NAME", "")
assert YOUR_TABLE_NAME, "TABLE_NAME environment variable is missing from .env"

# Run configuration
BABY_NAME = os.getenv("BABY_NAME", "BabyMonkeyAGI")
COOPERATIVE_MODE = "none"
JOIN_EXISTING_OBJECTIVE = False

# Goal configuation
OBJECTIVE = os.getenv("OBJECTIVE", "")
# Pinecone namespaces are only compatible with ascii characters (used in query and upsert)
ASCII_ONLY = re.compile('[^\x00-\x7F]+')
OBJECTIVE_PINECONE_COMPAT = re.sub(ASCII_ONLY, '', OBJECTIVE)

INITIAL_TASK = os.getenv("INITIAL_TASK", os.getenv("FIRST_TASK", ""))

# Model configuration
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", 0.0))


# Extensions support begin

def can_import(module_name):
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


DOTENV_EXTENSIONS = os.getenv("DOTENV_EXTENSIONS", "").split(" ")

# Command line arguments extension
# Can override any of the above environment variables
ENABLE_COMMAND_LINE_ARGS = (
    os.getenv("ENABLE_COMMAND_LINE_ARGS", "false").lower() == "true"
)
if ENABLE_COMMAND_LINE_ARGS:
    if can_import("extensions.argparseext"):
        from extensions.argparseext import parse_arguments

        OBJECTIVE, INITIAL_TASK, OPENAI_API_MODEL, DOTENV_EXTENSIONS, BABY_NAME, COOPERATIVE_MODE, JOIN_EXISTING_OBJECTIVE = parse_arguments()

# Load additional environment variables for enabled extensions
if DOTENV_EXTENSIONS:
    if can_import("extensions.dotenvext"):
        from extensions.dotenvext import load_dotenv_extensions

        load_dotenv_extensions(DOTENV_EXTENSIONS)


# TODO: There's still work to be done here to enable people to get
# defaults from dotenv extensions # but also provide command line
# arguments to override them

# Extensions support end

print("\033[95m\033[1m"+"\n*****CONFIGURATION*****\n"+"\033[0m\033[0m")
print(f"Name: {BABY_NAME}")
print(f"LLM : {OPENAI_API_MODEL}")
print(f"Mode: {'none' if COOPERATIVE_MODE in ['n', 'none'] else 'local' if COOPERATIVE_MODE in ['l', 'local'] else 'distributed' if COOPERATIVE_MODE in ['d', 'distributed'] else 'undefined'}")

# Check if we know what we are doing
assert OBJECTIVE, "OBJECTIVE environment variable is missing from .env"
assert INITIAL_TASK, "INITIAL_TASK environment variable is missing from .env"

if "gpt-4" in OPENAI_API_MODEL.lower():
    print(
        "\033[91m\033[1m"
        + "\n*****USING GPT-4. POTENTIALLY EXPENSIVE. MONITOR YOUR COSTS*****"
        + "\033[0m\033[0m"
    )

print("\033[94m\033[1m" + "\n*****OBJECTIVE*****\n" + "\033[0m\033[0m")
print(f"{OBJECTIVE}")

if not JOIN_EXISTING_OBJECTIVE: print("\033[93m\033[1m" + "\nInitial task:" + "\033[0m\033[0m" + f" {INITIAL_TASK}")
else: print("\033[93m\033[1m" + f"\nJoining to help the objective" + "\033[0m\033[0m")

# Configure OpenAI and Pinecone
openai.api_key = OPENAI_API_KEY
pinecone.init(api_key=PINECONE_API_KEY, environment=PINECONE_ENVIRONMENT)

# Create Pinecone index
table_name = YOUR_TABLE_NAME
dimension = 1536
metric = "cosine"
pod_type = "p1"
if table_name not in pinecone.list_indexes():
    pinecone.create_index(
        table_name, dimension=dimension, metric=metric, pod_type=pod_type
    )

# Connect to the index
index = pinecone.Index(table_name)

# Task storage supporting only a single instance of BabyAGI
class SingleTaskListStorage:
    def __init__(self):
        self.tasks = deque([])
        self.task_id_counter = 0

    def append(self, task: Dict):
        self.tasks.append(task)

    def replace(self, tasks: List[Dict]):
        self.tasks = deque(tasks)

    def popleft(self):
        return self.tasks.popleft()

    def is_empty(self):
        return False if self.tasks else True

    def next_task_id(self):
        self.task_id_counter += 1
        return self.task_id_counter

    def get_task_names(self):
        return [t["task_name"] for t in self.tasks]


# Initialize tasks storage
tasks_storage = SingleTaskListStorage()
if COOPERATIVE_MODE in ['l', 'local']:
    import sys
    from pathlib import Path
    sys.path.append(str(Path(__file__).resolve().parent))
    from extensions.ray_tasks import CooperativeTaskListStorage
    tasks_storage = CooperativeTaskListStorage(OBJECTIVE)
elif COOPERATIVE_MODE in ['d', 'distributed']:
    pass


# Get embedding for the text
def get_ada_embedding(text):
    text = text.replace("\n", " ")
    return openai.Embedding.create(input=[text], model="text-embedding-ada-002")[
        "data"
    ][0]["embedding"]


def openai_call(
    prompt: str,
    model: str = OPENAI_API_MODEL,
    temperature: float = OPENAI_TEMPERATURE,
    max_tokens: int = 100,
):
    while True:
        try:
            if model.startswith("llama"):
                # Spawn a subprocess to run llama.cpp
                cmd = ["llama/main", "-p", prompt]
                result = subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
                return result.stdout.strip()
            elif not model.startswith("gpt-"):
                # Use completion API
                response = openai.Completion.create(
                    engine=model,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    top_p=1,
                    frequency_penalty=0,
                    presence_penalty=0,
                )
                return response.choices[0].text.strip()
            else:
                # Use chat completion API
                messages = [{"role": "system", "content": prompt}]
                response = openai.ChatCompletion.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    n=1,
                    stop=None,
                )
                return response.choices[0].message.content.strip()
        except openai.error.RateLimitError:
            print(
                "   *** The OpenAI API rate limit has been exceeded. Waiting 10 seconds and trying again. ***"
            )
            time.sleep(10)  # Wait 10 seconds and try again
        except openai.error.Timeout:
            print(
                "   *** OpenAI API timeout occured. Waiting 10 seconds and trying again. ***"
            )
            time.sleep(10)  # Wait 10 seconds and try again
        except openai.error.APIError:
            print(
                "   *** OpenAI API error occured. Waiting 10 seconds and trying again. ***"
            )
            time.sleep(10)  # Wait 10 seconds and try again
        except openai.error.APIConnectionError:
            print(
                "   *** OpenAI API connection error occured. Check your network settings, proxy configuration, SSL certificates, or firewall rules. Waiting 10 seconds and trying again. ***"
            )
            time.sleep(10)  # Wait 10 seconds and try again
        except openai.error.InvalidRequestError:
            print(
                "   *** OpenAI API invalid request. Check the documentation for the specific API method you are calling and make sure you are sending valid and complete parameters. Waiting 10 seconds and trying again. ***"
            )
            time.sleep(10)  # Wait 10 seconds and try again
        except openai.error.ServiceUnavailableError:
            print(
                "   *** OpenAI API service unavailable. Waiting 10 seconds and trying again. ***"
            )
            time.sleep(10)  # Wait 10 seconds and try again
        else:
            break
        

class Thought:
    def __init__(self, task_name, parent=None):
        self.task_name = task_name
        self.parent = parent
        self.children = []

    def add_child(self, child):
        self.children.append(child)

class ThoughtTree:
    def __init__(self):
        self.root = None

    def add_thought(self, task_name, parent=None):
        new_thought = Thought(task_name, parent)
        if parent:
            parent.add_child(new_thought)
        else:
            self.root = new_thought
        return new_thought

    def get_thoughts(self, thought):
        return [thought.task_name] + [self.get_thoughts(child) for child in thought.children]

def generate_tasks_based_on_thought(thought):
    # This function should generate tasks based on the given thought.
    # The exact implementation will depend on the specifics of your problem.
    # For example, you might use a language model to generate potential continuations of the thought.

    task_map = {
        "INITIAL_TASK": ["Define problem", "Gather resources", "Develop plan"],
        "Define problem": ["Identify key components", "Determine constraints", "Set goals"],
        "Gather resources": ["Search for information", "Organize resources", "Evaluate resources"],
        "Develop plan": ["Outline steps", "Assign responsibilities", "Set timeline"],
        # ... add more tasks based on your problem ...
    }

    return task_map.get(thought.task_name, [])

def task_creation_agent(objective: str, result: Dict, task_description: str, thought_tree: ThoughtTree):
    # ... generate new tasks based on the branches of the current thought ...
    current_thought = thought_tree.root
    new_tasks = generate_tasks_based_on_thought(current_thought)
    if new_tasks is None:
        return []
    for task_name in new_tasks:
        thought_tree.add_thought(task_name, parent=current_thought)
    return new_tasks


def prioritization_agent():
    task_names = tasks_storage.get_task_names()
    next_task_id = tasks_storage.next_task_id()
    prompt = f"""
    You are a task prioritization AI tasked with cleaning the formatting of and reprioritizing the following tasks: {task_names}.
    Consider the ultimate objective of your team:{OBJECTIVE}.
    Do not remove any tasks. Return the result as a numbered list, like:
    #. First task
    #. Second task
    Start the task list with number {next_task_id}."""
    response = openai_call(prompt)
    new_tasks = response.split("\n") if "\n" in response else [response]
    new_tasks_list = []
    for task_string in new_tasks:
        task_parts = task_string.strip().split(".", 1)
        if len(task_parts) == 2:
            task_id = task_parts[0].strip()
            task_name = task_parts[1].strip()
            new_tasks_list.append({"task_id": task_id, "task_name": task_name})
    tasks_storage.replace(new_tasks_list)


def explore_thought(thought):
    # This function should explore the given thought to execute the corresponding task.
    # The exact implementation will depend on the specifics of your problem.
    # For example, you might use a language model to generate a response based on the thought.

    return openai_call(thought.task_name)


# Execute a task based on the objective and five previous tasks 
def execution_agent(objective: str, thought: Thought):
    # ... execute the task by exploring the branches of the current thought ...
    result = explore_thought(thought)
    return result


# Get the top n completed tasks for the objective
def context_agent(query: str, top_results_num: int):
    """
    Retrieves context for a given query from an index of tasks.

    Args:
        query (str): The query or objective for retrieving context.
        top_results_num (int): The number of top results to retrieve.

    Returns:
        list: A list of tasks as context for the given query, sorted by relevance.

    """
    query_embedding = get_ada_embedding(query)
    results = index.query(query_embedding, top_k=top_results_num, include_metadata=True, namespace=OBJECTIVE_PINECONE_COMPAT)
    # print("***** RESULTS *****")
    # print(results)
    sorted_results = sorted(results.matches, key=lambda x: x.score, reverse=True)
    return [(str(item.metadata["task"])) for item in sorted_results]

# Add the initial task if starting new objective
if not JOIN_EXISTING_OBJECTIVE:
    initial_task = {
        "task_id": tasks_storage.next_task_id(),
        "task_name": INITIAL_TASK
    }
    tasks_storage.append(initial_task)


def get_next_thought(thought_tree):
    # This function gets the next thought to be executed.
    # The exact implementation will depend on the specifics of your problem.
    # For example, you might use a breadth-first search or depth-first search algorithm to traverse the thought tree.
    # For simplicity, let's assume we're using a depth-first search and we're storing the thoughts in a stack.
    stack = [thought_tree.root]
    while stack:
        thought = stack.pop()
        if not thought.children:
            return thought
        stack.extend(thought.children)
    return None


def is_thought_tree_empty(thought_tree):
    # This function checks if the thought tree is empty.
    # The thought tree is empty if it has no root.
    return thought_tree.root is None


def main ():
    thought_tree = ThoughtTree()
    thought_tree.add_thought(INITIAL_TASK)
    while not is_thought_tree_empty(thought_tree):
        current_thought = get_next_thought(thought_tree)
        # Print the task list
        print("\033[95m\033[1m" + "\n*****TASK LIST*****\n" + "\033[0m\033[0m")
        for t in tasks_storage.get_task_names():
            print(" • "+t)
        # Step 1: Pull the first incomplete task
        task = tasks_storage.popleft()
        print("\033[92m\033[1m" + "\n*****NEXT TASK*****\n" + "\033[0m\033[0m")
        print(task['task_name'])
        # Send to execution function to complete the task based on the context
        result = execution_agent(OBJECTIVE, current_thought)
        print("\033[93m\033[1m" + "\n*****TASK RESULT*****\n" + "\033[0m\033[0m")
        print(result)
        # Step 2: Enrich result and store in Pinecone
        enriched_result = {
            "data": result
        }  # This is where you should enrich the result if needed
        result_id = f"result_{task['task_id']}"
        vector = get_ada_embedding(
            enriched_result["data"]
        )  # get vector of the actual result extracted from the dictionary
        index.upsert(
            [(result_id, vector, {"task": task["task_name"], "result": result})],
          nmespace=OBJECTIVE_PINECONE_COMPAT
        )
        # Step 3: Create new tasks and reprioritize task list
        new_tasks = task_creation_agent(OBJECTIVE, result, current_thought.task_name, thought_tree)
        for task_name in new_tasks:
            new_task = {"task_name": task_name, "task_id": tasks_storage.next_task_id()}
            # new_task.update({"task_id": tasks_storage.next_task_id()})
            tasks_storage.append(new_task)
        if not JOIN_EXISTING_OBJECTIVE: prioritization_agent()

        time.sleep(5)  # Sleep before checking the task list again

if __name__ == "__main__":
    main()
