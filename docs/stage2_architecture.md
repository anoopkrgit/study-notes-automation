# Stage-2 Agentic Architecture Design

This document outlines the architecture for the **Stage 2 Agentic Implementation**. 
The design clearly separates non-LLM steps from LLM Agents, and shows how they are connected.

## Architectural Layers

```mermaid
flowchart TD
    %% Define Styles
    classDef agent fill:#d4edda,stroke:#28a745,stroke-width:2px,color:#155724
    classDef scriptNode fill:#e2e3e5,stroke:#6c757d,stroke-width:2px,color:#383d41
    classDef core fill:#cce5ff,stroke:#007bff,stroke-width:2px,color:#004085
    
    subgraph Graph ["LangGraph Orchestration (stage2_graph.py)"]
        direction TB
        N1["Ingest Node<br>(No LLM)"]:::scriptNode
        N2["Author Agent<br>(Claude Sonnet)"]:::agent
        N3["Figure Agent<br>(Claude Haiku)"]:::agent
        N4["Compiler Agent<br>(Claude Haiku)"]:::agent
        N5["QA Node<br>(Deterministic Gates)"]:::scriptNode
        
        N1 --> N2
        N2 --> N3
        N3 --> N4
        N4 --> N5
        N5 -- "QA Failed<br>(Retry)" --> N2
        N5 -- "QA Passed" --> Done((End))
    end
    
    subgraph Engine ["Agent Engine (base.py)"]
        Factory["make_agent_node<br>(LLM Factory & Message Loop)"]:::core
        Anthropic["Anthropic API"]:::core
        Tools["Tool Wrapper API<br>(tools.py)"]:::core
        
        N2 -.-> |Uses| Factory
        N3 -.-> |Uses| Factory
        N4 -.-> |Uses| Factory
        
        Factory -- "Requests" --> Anthropic
        Anthropic -- "JSON Responses" --> Factory
        Factory -- "Executes" --> Tools
    end
```

## Layer Breakdown

### 1. LangGraph Orchestration (Stage 2 Graph)
- **Ingest & QA Nodes (Grey)**: These are completely deterministic. They do not use the LLM. Ingest prepares files, and QA runs rigorous python scripts to validate the outputs.
- **Agents (Green)**: The Author, Figure, and Compiler nodes are LLM-powered agents. 
- **The Flow**: Proceeds linearly until QA. If QA finds structural or quality failures, the flow loops back to the Author Agent to fix the errors.

### 2. Agent Engine
- **`make_agent_node` (Blue)**: Instead of copy-pasting the LLM API logic, this central factory runs the message loop for all the Agents. It talks to Anthropic and handles tool calls securely.
- **`tools.py`**: A safe wrapper around your legacy Bash/Python/Node scripts, so the Agents can only execute allowed commands.
