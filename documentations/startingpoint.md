# Vistui

A rest API application to store and retrieve memory from conversation with chatbots.
It's mostly intended for long chat with AI Companions or roleplay but it should be compatible with all chatbot (like openwebui). 
It's agnostic of the backend as long as the data is send correctly which would be the job of extension/mcp not the rest API itself.
The whole thing should be quite fast and seemless on retrival and use asynchro batch jobs to consolidate the memory when no active conversation is running.
The idea is to have a good memory but this memory doesn't need to be built right away as the context window on the chat bot should be good enough to remember recent stuff. So we can do batch memory saving when there is low activity. This is ok because it's intented to be a self hosted app by individuals not a enterprise one.


## Architecture document focus

On a high level abstraction. A few tech will be explicitly named but it's not listing all the tech requirements only the core one

## Core tech

- uv/devbox/direnv/docker/justfile to manage the dev env 
- Postgres with pgvector for storage
- python/fastapi/pydantic/sqlsalchemy for the rest api
- pytest/polyfactory/mutmut for test
- langchain to call llm

## Overview of the memory system

### Objects
The memory system is based on multiple objets.

Some object have a salience score.

The salience score is determined by a llm when generating a memory. The exact instruction will be in a txt file but in short it should help with scoring on retrival


#### User

The User. with all regular data for a user

#### ChatGroup 

Optionnal a grouping created by the user to retrieve memory from multiple chat.
Linked to a user. Unique id, a name, a description, configs with default values

#### Chat 

Represent the chat window with the chatbot. Linked to a user and a chatgroup
Unique id, a name, a description
If not added to a chatgroup it's creation trigger the creation of a new chatgroup with the same name and description as the chat

#### Message

A message in the  chat.
Unique id. Full text, keywords, embedding, role (system, user, assistant), timestamps, salience score
Linked to a chat
linked to a prevmessage and nextmessage
Can be fully rewriten (to erase swipes and edit we don't want the previous version in the memory)

#### Event
Time bounded.
Summary of mutitple contiguous messages.
Rewriten often but only the current one is rewriten
Link to multiple messages. Unique id, full text, keywords. embedding, start and end timestamps, salience score

#### Topic
Summary of a topic.
Links to multiple non contiguous message 
Rewriten when the topic is revisited.
Link to multiple messages unique id, full text, keywords, embedding, start and last edit time stamp, salience score

#### Fact
Small text about a fact to be remember
Rewriten when the fact change (ie: Mom is sick => Mom is on vaccation)
Link to multiple messages (the latest one is the relevant one, all the rest is history), unique id, full text, keyworks, embedding, start and last edit time stamp, salience score


### Retrival workflow

New message -> API REST -> generate embedding and keywords for search and memory ratio -> save the message -> search in the database -> creating the response -> sending the response

### generate embedding and keywords for search and send ratio

All in parallel to save time.

##### Generate embedding

Call an openai api to generate the embeding 

##### keywords for search

Call an llm to generate keywords to search from the message
this will be tweaked by a prompt in a txt file

##### memory ratio

Call an llm to generate the memory ratio of stuff return in function of the message (ie: broad vs narrow focus : 40% messages, 10% events, 30% events, 20% facts)
this will be tweaked by a prompt in a txt file

#### Save the message in db

trivial

#### Search the database

Vector search plus text search with BM25
Merge and calculate the relevance of each result (mathematical formula to determine, possibly in the chatgroup config)


#### Creating the response

Given the result of the search. The constrains (config with nb of token return or param send in the api + the memory ratio) => compute how many token per Message/Event/Topic/Fact returned

Fill them each in fonction of the relevance until it reach the max amount of token allowed

#### sending the response

trival

### Batch job

This should probably trigger after one hours without receiving any message and should be paused if a message is received. 
In any case it should be able to start and stop an any time in most state.
The past x messages(configured in chat) are never processed (to avoid complication if the user update or reroll the message it's basically hell for us, it could mess the fact)

#### Message

Message are injected and embedded imediatly when received however they aren't processed. Each message have a the info if it have been processed for event, topic and fact in addition of it's general data.

#### Event

When a message is processed for an event its send to an llm alongside all messages for the event.
The llm must generate a summary for the current event and eventually for a new event.
This will be tweaked by a txt file
Once a event is "finished" (ie, not the last one). This event is send to compute the embedding

#### Topic

When a message is processed for a topic a search is made to retrieve all the topic that could be updated for the message.
The topics with a score higher than (config in chatgroup) will be updated. Each topic is send to an llm alongside the message and the llm have the instruction to update the topic if relevant.
Updated topics are saved and send to compute the embedding

#### Fact
When a message is processed for a fact a search is made to retrieve all fact that could be updated for the message.
The facts  with a score higher than (config in chatgroup) will be send to a llm alongside the message. This llm will determine up to 5 facts to be updated. Tweaked by a prompt in txt
Each fact will be send to a llm alongside the message to process and the past x messages (configure on chatgroup) of the fact. The llm will choose to update the fact or not. Tweaked by a prompt in txt.
If the fact is updated it will be send to embedding
 
