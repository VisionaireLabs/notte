from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from litellm.files.main import ModelResponse
from loguru import logger
from typing_extensions import override

from notte.actions.base import Action, PossibleAction
from notte.actions.parsing import ActionListingParser
from notte.actions.space import ActionSpace
from notte.browser.context import Context
from notte.llms.engine import StructuredContent
from notte.llms.service import LLMService


class BaseActionListingPipe(ABC):

    def __init__(self, llmserve: LLMService | None = None) -> None:
        self.llmserve: LLMService = llmserve or LLMService()

    @abstractmethod
    def forward(self, context: Context, previous_action_list: list[Action] | None = None) -> list[PossibleAction]:
        pass

    @abstractmethod
    def forward_incremental(
        self,
        context: Context,
        previous_action_list: list[Action],
    ) -> list[PossibleAction]:
        raise NotImplementedError("forward_incremental")


class BaseSimpleActionListingPipe(BaseActionListingPipe, ABC):

    def __init__(
        self,
        llmserve: LLMService | None,
        prompt_id: str,
        incremental_prompt_id: str,
        parser: ActionListingParser,
    ) -> None:
        super().__init__(llmserve)
        self.parser: ActionListingParser = parser
        self.prompt_id: str = prompt_id
        self.incremental_prompt_id: str = incremental_prompt_id

    @abstractmethod
    def get_prompt_variables(
        self, context: Context, previous_action_list: list[Action] | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError("get_prompt_variables")

    def parse_llm_response(self, response: ModelResponse) -> list[PossibleAction]:
        sc = StructuredContent(outer_tag="action-listing")
        if response.choices[0].message.content is None:  # type: ignore
            raise ValueError("No content in response")
        text = sc.extract(response.choices[0].message.content)  # type: ignore
        return self.parser.parse(text)

    @override
    def forward(
        self,
        context: Context,
        previous_action_list: list[Action] | None = None,
    ) -> list[PossibleAction]:
        variables = self.get_prompt_variables(context, previous_action_list)
        response = self.llmserve.completion(self.prompt_id, variables)
        return self.parse_llm_response(response)

    @override
    def forward_incremental(
        self,
        context: Context,
        previous_action_list: list[Action],
    ) -> list[PossibleAction]:
        incremental_context = context.subgraph_without(previous_action_list)
        variables = self.get_prompt_variables(incremental_context, previous_action_list)
        response = self.llmserve.completion(self.incremental_prompt_id, variables)
        return self.parse_llm_response(response)


class RetryPipeWrapper(BaseActionListingPipe):
    def __init__(
        self,
        pipe: BaseActionListingPipe,
        max_tries: int = 3,
    ):
        super().__init__(pipe.llmserve)
        self.pipe: BaseActionListingPipe = pipe
        self.max_tries: int = max_tries

    @override
    def forward(self, context: Context, previous_action_list: list[Action] | None = None) -> list[PossibleAction]:
        errors: list[str] = []
        for _ in range(self.max_tries):
            try:
                return self.pipe.forward(context, previous_action_list)
            except Exception as e:
                logger.warning("failed to parse action list but retrying...")
                errors.append(str(e))
        raise Exception(f"Failed to get action list after max tries with errors: {errors}")

    @override
    def forward_incremental(
        self,
        context: Context,
        previous_action_list: list[Action],
    ) -> list[PossibleAction]:
        for _ in range(self.max_tries):
            try:
                return self.pipe.forward_incremental(context, previous_action_list)
            except Exception:
                pass
        logger.error("Failed to get action list after max tries => returning previous action list")
        return [
            PossibleAction(
                id=act.id,
                description=act.description,
                category=act.category,
                params=act.params,
            )
            for act in previous_action_list
        ]


class MarkdownTableActionListingPipe(BaseSimpleActionListingPipe):

    def __init__(self, llmserve: LLMService | None = None) -> None:
        super().__init__(
            llmserve=llmserve,
            prompt_id="action-listing/optim",
            incremental_prompt_id="action-listing-incr",
            parser=ActionListingParser.TABLE,
        )

    @override
    def get_prompt_variables(
        self, context: Context, previous_action_list: list[Action] | None = None
    ) -> dict[str, Any]:
        vars = {"document": context.markdown_description()}
        if previous_action_list is not None:
            vars["previous_action_list"] = ActionSpace(_actions=previous_action_list).markdown(
                "all", include_special=False
            )
        return vars


class ActionListingPipe(Enum):
    SIMPLE_MARKDOWN_TABLE = "simple-markdown-table"

    def get_pipe(self, llmserve: LLMService | None = None) -> BaseActionListingPipe:
        match self.value:
            case ActionListingPipe.SIMPLE_MARKDOWN_TABLE.value:
                pipe = MarkdownTableActionListingPipe(llmserve)
            case _:
                raise ValueError(f"Unknown pipe name: {self.value}")
        return RetryPipeWrapper(pipe)
