"""A scripted chat model that can 'call tools', for offline end-to-end agent tests."""

import itertools

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

_ids = itertools.count(1)


class ScriptedModel(GenericFakeChatModel):
    """Replies with the scripted messages in order; tool binding is a no-op."""

    def bind_tools(self, tools, **kwargs):
        return self


def scripted(*messages: AIMessage) -> ScriptedModel:
    return ScriptedModel(messages=iter(messages))


def call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args,
                                              "id": f"call_{next(_ids)}"}])


def say(text: str) -> AIMessage:
    return AIMessage(content=text)
