
from dotenv import load_dotenv
from langchain import hub
from langchain.output_parsers import PydanticOutputParser
from langchain_core.output_parsers import StrOutputParser
from langchain.schema import Document
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_community.document_loaders import WebBaseLoader
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_community.vectorstores import Chroma
from langchain_community.chat_models import ChatOllama
from langchain_community.embeddings import GPT4AllEmbeddings
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langgraph.graph import END, StateGraph
from typing import Dict, TypedDict
from langchain.prompts import PromptTemplate
import pprint
import os
import streamlit as st


# local 

run_local = 'No'
local_llm = 'mistral'

# online

load_dotenv()

models = "openai" # Google...

openai_api_key = os.environ.get("OPENAI_API_KEY")
openai_api_base= os.environ.get("OPENAI_API_BASE")
google_api_key = "you api"
tavily_api_key = os.environ.get("TAVILY_API_KEY")


# Split documents
url  = 'https://lilianweng.github.io/posts/2023-06-23-agent/'
loader = WebBaseLoader(url)
docs = loader.load()


# Split
text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
    chunk_size=500, chunk_overlap=100
)
all_splits = text_splitter.split_documents(docs)


# Embed and index
if run_local == 'Yes':
    embeddings = GPT4AllEmbeddings()
elif models == 'openai':
    embeddings = OpenAIEmbeddings(openai_api_key=openai_api_key, openai_api_base=openai_api_base)
else:
    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/embedding-001", google_api_key=google_api_key
    )

# Index
vectorstore = Chroma.from_documents(
    documents=all_splits,
    collection_name="rag-chroma",
    embedding=embeddings,
)
retriever = vectorstore.as_retriever()


class GraphState(TypedDict):
    """
    Represents the state of our graph.

    Attributes:
        keys: A dictionary where each key is a string.
    """

    keys: Dict[str, any]


def retrieve(state):
    """
    Retrieve documents

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): New key added to state, documents, 
        that contains retrieved documents
    """
    print("---RETRIEVE---")
    state_dict = state["keys"]
    question = state_dict["question"]
    local = state_dict["local"]
    documents = retriever.get_relevant_documents(question)
    return {"keys": {"documents": documents, "local": local, 
            "question": question}}


def generate(state):
    """
    Generate answer

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): New key added to state, generation, 
        that contains LLM generation
    """
    print("---GENERATE---")
    state_dict = state["keys"]
    question = state_dict["question"]
    documents = state_dict["documents"]

    # Prompt
    prompt = hub.pull("rlm/rag-prompt")

    # LLM Setup
    if run_local == "Yes":
        llm = ChatOllama(model=local_llm, 
                        temperature=0)
    elif models == "openai" :
        llm = ChatOpenAI(
            model="gpt-3.5-turbo",  # gpt-4-0125-preview
            temperature=0 , 
            openai_api_key=openai_api_key
        )
    else:
        llm = ChatGoogleGenerativeAI(model="gemini-pro",
                                    google_api_key=google_api_key,
                                    convert_system_message_to_human = True,
                                    verbose = True,
        )

    # Post-processing
    def format_docs(docs):
        return "\n\n".join(doc.page_content for doc in docs)

    # Chain
    rag_chain = prompt | llm | StrOutputParser()

    # Run
    generation = rag_chain.invoke({"context": documents, 
                                  "question": question})
    return {
        "keys": {"documents": documents, "question": question, 
                               "generation": generation}
    }


def grade_documents(state):
    """
    Determines whether the retrieved documents are relevant to the question.

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): Updates documents key with relevant documents
    """

    print("---CHECK RELEVANCE---")
    state_dict = state["keys"]
    question = state_dict["question"]
    documents = state_dict["documents"]
    local = state_dict["local"]

    # LLM
    if run_local == "Yes":
        llm = ChatOllama(model=local_llm, 
                        temperature=0)
    elif models == "openai" :
        llm = ChatOpenAI(
            model="gpt-4-0125-preview", 
            temperature=0 , 
            openai_api_key=openai_api_key
        )
    else:
        llm = ChatGoogleGenerativeAI(model="gemini-pro",
                                    google_api_key=google_api_key,
                                    convert_system_message_to_human = True,
                                    verbose = True,
        )
    # Data model
    class grade(BaseModel):
        """Binary score for relevance check."""

        score: str = Field(description="Relevance score 'yes' or 'no'")

    # Set up a parser + inject instructions into the prompt template.
    parser = PydanticOutputParser(pydantic_object=grade)

    from langchain_core.output_parsers import JsonOutputParser

    parser = JsonOutputParser(pydantic_object=grade)

    prompt = PromptTemplate(
        template="""You are a grader assessing relevance of a retrieved 
                     document to a user question. \n 
        Here is the retrieved document: \n\n {context} \n\n
        Here is the user question: {question} \n
        If the document contains keywords related to the user question, 
           grade it as relevant. \n
        It does not need to be a stringent test. The goal is to filter out 
        erroneous retrievals. \n
        Give a binary score 'yes' or 'no' score to indicate whether the 
        document is relevant to the question. \n
        Provide the binary score as a JSON with no premable or 
        explaination and use these instructons to format the output: 
        {format_instructions}""",
        input_variables=["query"],
        partial_variables={"format_instructions":
                  parser.get_format_instructions()},
    )

    chain = prompt | llm | parser

    # Score
    filtered_docs = []
    search = "No"  # Default do not opt for web search to supplement retrieval
    for d in documents:
        score = chain.invoke(
            {
                "question": question,
                "context": d.page_content,
                "format_instructions": parser.get_format_instructions(),
            }
        )
        grade = score["score"]
        if grade == "yes":
            print("---GRADE: DOCUMENT RELEVANT---")
            filtered_docs.append(d)
        else:
            print("---GRADE: DOCUMENT NOT RELEVANT---")
            search = "Yes"  # Perform web search
            continue

    return {
        "keys": {
            "documents": filtered_docs,
            "question": question,
            "local": local,
            "run_web_search": search,
        }
    }


def transform_query(state):
    """
    Transform the query to produce a better question.

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): Updates question key with a re-phrased question
    """

    print("---TRANSFORM QUERY---")
    state_dict = state["keys"]
    question = state_dict["question"]
    documents = state_dict["documents"]
    local = state_dict["local"]

    # Create a prompt template with format instructions and the query
    prompt = PromptTemplate(
        template="""You are generating questions that is well optimized for 
                    retrieval. \n 
        Look at the input and try to reason about the underlying sematic 
        intent / meaning. \n 
        Here is the initial question:
        \n ------- \n
        {question} 
        \n ------- \n
        Provide an improved question without any premable, only respond 
        with the updated question: """,
        input_variables=["question"],
    )

    # Grader
    # LLM
    if run_local == "Yes":
        llm = ChatOllama(model=local_llm, 
                        temperature=0)
    elif models == "openai" :
        llm = ChatOpenAI(
            model="gpt-4-0125-preview", 
            temperature=0 , 
            openai_api_key=openai_api_key
        )
    else:
        llm = ChatGoogleGenerativeAI(model="gemini-pro",
                                    google_api_key=google_api_key,
                                    convert_system_message_to_human = True,
                                    verbose = True,
        )
    # Prompt
    chain = prompt | llm | StrOutputParser()
    better_question = chain.invoke({"question": question})

    return {
        "keys": {"documents": documents, "question": better_question, 
        "local": local}
    }


def web_search(state):
    """
    Web search based on the re-phrased question using Tavily API.

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): Web results appended to documents.
    """

    print("---WEB SEARCH---")
    state_dict = state["keys"]
    question = state_dict["question"]
    documents = state_dict["documents"]
    local = state_dict["local"]
    try:
        tool = TavilySearchResults()
        docs = tool.invoke({"query": question})
        web_results = "\n".join([d["content"] for d in docs])
        web_results = Document(page_content=web_results)
        documents.append(web_results)
    except Exception as error:
        print(error)

    return {"keys": {"documents": documents, "local": local,
    "question": question}}


def decide_to_generate(state):
    """
    Determines whether to generate an answer or re-generate a question 
    for web search.

    Args:
        state (dict): The current state of the agent, including all keys.

    Returns:
        str: Next node to call
    """

    print("---DECIDE TO GENERATE---")
    state_dict = state["keys"]
    question = state_dict["question"]
    filtered_documents = state_dict["documents"]
    search = state_dict["run_web_search"]

    if search == "Yes":
        # All documents have been filtered check_relevance
        # We will re-generate a new query
        print("---DECISION: TRANSFORM QUERY and RUN WEB SEARCH---")
        return "transform_query"
    else:
        # We have relevant documents, so generate answer
        print("---DECISION: GENERATE---")
        return "generate"


workflow = StateGraph(GraphState)

# Define the nodes
workflow.add_node("retrieve", retrieve)  # retrieve
workflow.add_node("grade_documents", grade_documents)  # grade documents
workflow.add_node("generate", generate)  # generatae
workflow.add_node("transform_query", transform_query)  # transform_query
workflow.add_node("web_search", web_search)  # web search

# Build graph
workflow.set_entry_point("retrieve")
workflow.add_edge("retrieve", "grade_documents")
workflow.add_conditional_edges(
    "grade_documents",
    decide_to_generate,
    {
        "transform_query": "transform_query",
        "generate": "generate",
    },
)
workflow.add_edge("transform_query", "web_search")
workflow.add_edge("web_search", "generate")
workflow.add_edge("generate", END)

# Compile
app = workflow.compile()


# Run

st.title("Langgraph Demo")

st.text("A possible query: Explain how the different types of agent memory work?")

# 用户输入问题
user_question = st.text_input("Please enter your question:")
# Explain how the different types of agent memory work?

if user_question:
    inputs = {
        "keys": {
            "question": user_question,
            "local": run_local,
        }
    }

    # 对于每个节点的输出，创建一个st.expander UI组件
    for output in app.stream(inputs):
        for key, value in output.items():
            # 创建一个可展开的UI块，标题是节点名称
            with st.expander(f"Node '{key}':"):
                # 在expander内部，显示该节点的详细信息
                # 使用pprint格式化字典数据，使其易于阅读
                st.text(pprint.pformat(value["keys"], indent=2, width=80, depth=None))

    # 当所有节点都处理完毕后，显示最终生成的内容
    final_generation = value['keys'].get('generation', 'No final generation produced.')
    st.subheader("Final Generation:")
    st.write(final_generation)