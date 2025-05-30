"""Azure AI embeddings model inference API."""

import logging
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    Generator,
    Mapping,
    Optional,
    Union,
)

from azure.ai.inference import EmbeddingsClient
from azure.ai.inference.aio import EmbeddingsClient as EmbeddingsClientAsync
from azure.ai.inference.models import EmbeddingInputType
from azure.core.credentials import AzureKeyCredential, TokenCredential
from azure.core.exceptions import HttpResponseError
from langchain_core.embeddings import Embeddings
from langchain_core.utils import get_from_dict_or_env, pre_init
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from langchain_azure_ai.utils.utils import get_endpoint_from_project

logger = logging.getLogger(__name__)


class AzureAIEmbeddingsModel(BaseModel, Embeddings):
    """Azure AI model inference for embeddings.

    Examples:
        .. code-block:: python
            from langchain_azure_ai.embeddings import AzureAIEmbeddingsModel

            embed_model = AzureAIEmbeddingsModel(
                endpoint="https://[your-endpoint].inference.ai.azure.com",
                credential="your-api-key",
            )

        If your endpoint supports multiple models, indicate the parameter `model_name`:

        .. code-block:: python
            from langchain_azure_ai.embeddings import AzureAIEmbeddingsModel

            embed_model = AzureAIEmbeddingsModel(
                endpoint="https://[your-service].services.ai.azure.com/models",
                credential="your-api-key",
                model="cohere-embed-v3-multilingual"
            )

    Troubleshooting:
        To diagnostic issues with the model, you can enable debug logging:

        .. code-block:: python
            import sys
            import logging
            from langchain_azure_ai.embeddings import AzureAIEmbeddingsModel

            logger = logging.getLogger("azure")

            # Set the desired logging level.
            logger.setLevel(logging.DEBUG)

            handler = logging.StreamHandler(stream=sys.stdout)
            logger.addHandler(handler)

            model = AzureAIEmbeddingsModel(
                endpoint="https://[your-service].services.ai.azure.com/models",
                credential="your-api-key",
                model="cohere-embed-v3-multilingual",
                client_kwargs={ "logging_enable": True }
            )
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=())

    project_connection_string: Optional[str] = None
    """The connection string to use for the Azure AI project. If this is specified,
    then the `endpoint` parameter becomes optional and `credential` has to be of type
    `TokenCredential`."""

    endpoint: Optional[str] = None
    """The endpoint URI where the model is deployed. Either this or the
    `project_connection_string` parameter must be specified."""

    credential: Union[str, AzureKeyCredential, TokenCredential]
    """The API key or credential to use for the Azure AI model inference."""

    api_version: Optional[str] = None
    """The API version to use for the Azure AI model inference API. If None, the 
    default version is used."""

    model_name: Optional[str] = Field(default=None, alias="model")
    """The name of the model to use for inference, if the endpoint is running more 
    than one model. If not, this parameter is ignored."""

    embed_batch_size: int = 1024
    """The batch size for embedding requests. The default is 1024."""

    dimensions: Optional[int] = None
    """The number of dimensions in the embeddings to generate. If None, the model's 
    default is used."""

    model_kwargs: Dict[str, Any] = {}
    """Additional kwargs model parameters."""

    client_kwargs: Dict[str, Any] = {}
    """Additional kwargs for the Azure AI client used."""

    _client: EmbeddingsClient = PrivateAttr()
    _async_client: EmbeddingsClientAsync = PrivateAttr()
    _embed_input_type: Optional[EmbeddingInputType] = PrivateAttr()
    _model_name: Optional[str] = PrivateAttr()

    @pre_init
    def validate_environment(cls, values: Dict) -> Any:
        """Validate that api key exists in environment."""
        values["endpoint"] = get_from_dict_or_env(
            values, "endpoint", "AZURE_INFERENCE_ENDPOINT"
        )
        values["credential"] = get_from_dict_or_env(
            values, "credential", "AZURE_INFERENCE_CREDENTIAL"
        )

        if values["api_version"]:
            values["client_kwargs"]["api_version"] = values["api_version"]

        return values

    @model_validator(mode="after")
    def initialize_client(self) -> "AzureAIEmbeddingsModel":
        """Initialize the Azure AI model inference client."""
        if self.project_connection_string:
            if not isinstance(self.credential, TokenCredential):
                raise ValueError(
                    "When using the `project_connection_string` parameter, the "
                    "`credential` parameter must be of type `TokenCredential`."
                )
            self.endpoint, self.credential = get_endpoint_from_project(
                self.project_connection_string, self.credential
            )

        credential = (
            AzureKeyCredential(self.credential)
            if isinstance(self.credential, str)
            else self.credential
        )

        self._client = EmbeddingsClient(
            endpoint=self.endpoint,  # type: ignore[arg-type]
            credential=credential,  # type: ignore[arg-type]
            model=self.model_name,
            user_agent="langchain-azure-ai",
            **self.client_kwargs,
        )

        self._async_client = EmbeddingsClientAsync(
            endpoint=self.endpoint,  # type: ignore[arg-type]
            credential=credential,  # type: ignore[arg-type]
            model=self.model_name,
            user_agent="langchain-azure-ai",
            **self.client_kwargs,
        )

        if not self.model_name:
            try:
                # Get model info from the endpoint. This method may not be supported
                # by all endpoints.
                model_info = self._client.get_model_info()
                self._model_name = model_info.get("model_name", None)
                self._embed_input_type = (
                    None
                    if model_info.get("model_provider_name", None).lower() == "cohere"
                    else EmbeddingInputType.TEXT
                )
            except HttpResponseError:
                logger.warning(
                    f"Endpoint '{self.endpoint}' does not support model metadata "
                    "retrieval. Unable to populate model attributes."
                )
                self._model_name = ""
                self._embed_input_type = EmbeddingInputType.TEXT
        else:
            self._embed_input_type = (
                None if "cohere" in self.model_name.lower() else EmbeddingInputType.TEXT
            )

        return self

    def _get_model_params(self, **kwargs: Dict[str, Any]) -> Mapping[str, Any]:
        params: Dict[str, Any] = {}
        if self.dimensions:
            params["dimensions"] = self.dimensions
        if self.model_kwargs:
            params["model_extras"] = self.model_kwargs

        params.update(kwargs)
        return params

    def _embed(
        self, texts: list[str], input_type: EmbeddingInputType
    ) -> Generator[list[float], None, None]:
        for text_batch in range(0, len(texts), self.embed_batch_size):
            response = self._client.embed(
                input=texts[text_batch : text_batch + self.embed_batch_size],
                input_type=self._embed_input_type or input_type,
                **self._get_model_params(),
            )

            for data in response.data:
                yield data.embedding  # type: ignore

    async def _embed_async(
        self, texts: list[str], input_type: EmbeddingInputType
    ) -> AsyncGenerator[list[float], None]:
        for text_batch in range(0, len(texts), self.embed_batch_size):
            response = await self._async_client.embed(
                input=texts[text_batch : text_batch + self.embed_batch_size],
                input_type=self._embed_input_type or input_type,
                **self._get_model_params(),
            )

            for data in response.data:
                yield data.embedding  # type: ignore

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed search docs.

        Args:
            texts: List of text to embed.

        Returns:
            List of embeddings.
        """
        return list(self._embed(texts, EmbeddingInputType.DOCUMENT))

    def embed_query(self, text: str) -> list[float]:
        """Embed query text.

        Args:
            text: Text to embed.

        Returns:
            Embedding.
        """
        return list(self._embed([text], EmbeddingInputType.QUERY))[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """Asynchronous Embed search docs.

        Args:
            texts: List of text to embed.

        Returns:
            List of embeddings.
        """
        return self._embed_async(texts, EmbeddingInputType.DOCUMENT)  # type: ignore[return-value]

    async def aembed_query(self, text: str) -> list[float]:
        """Asynchronous Embed query text.

        Args:
            text: Text to embed.

        Returns:
            Embedding.
        """
        async for item in self._embed_async([text], EmbeddingInputType.QUERY):
            return item
        return []
