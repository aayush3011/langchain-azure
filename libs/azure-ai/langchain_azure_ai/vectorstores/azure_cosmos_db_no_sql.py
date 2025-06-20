"""Vector Store for CosmosDB NoSql."""

from __future__ import annotations

import uuid
import warnings
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Collection,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
)

import numpy as np
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import VectorStore, VectorStoreRetriever
from pydantic import ConfigDict, model_validator

from langchain_azure_ai.vectorstores.utils import maximal_marginal_relevance

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy, CosmosClient
    from azure.identity import DefaultAzureCredential

USER_AGENT = ("LangChain-CDBNoSql-VectorStore-Python",)


class AzureCosmosDBNoSqlVectorSearch(VectorStore):
    """`Azure Cosmos DB for NoSQL` vector store.

    To use, you should have both:
        - the ``azure-cosmos`` python package installed

    You can read more about vector search, full text search
    and hybrid search using AzureCosmosDBNoSQL here:
    https://learn.microsoft.com/en-us/azure/cosmos-db/nosql/vector-search
    https://learn.microsoft.com/en-us/azure/cosmos-db/gen-ai/full-text-search
    https://learn.microsoft.com/en-us/azure/cosmos-db/gen-ai/hybrid-search
    """

    def __init__(
        self,
        *,
        cosmos_client: CosmosClient,
        embedding: Embeddings,
        vector_embedding_policy: Dict[str, Any],
        indexing_policy: Dict[str, Any],
        cosmos_container_properties: Dict[str, Any],
        cosmos_database_properties: Dict[str, Any],
        vector_search_fields: Dict[str, Any],
        full_text_policy: Optional[Dict[str, Any]] = None,
        database_name: str = "vectorSearchDB",
        container_name: str = "vectorSearchContainer",
        search_type: str = "vector",
        metadata_key: str = "metadata",
        create_container: bool = True,
        full_text_search_enabled: bool = False,
        table_alias: str = "c",
    ):
        """Constructor for AzureCosmosDBNoSqlVectorSearch.

        Args:
            cosmos_client: Client used to connect to azure cosmosdb no sql account.
            database_name: Name of the database to be created.
            container_name: Name of the container to be created.
            embedding: Text embedding model to use.
            vector_embedding_policy: Vector Embedding Policy for the container.
            full_text_policy: Full Text Policy for the container.
            indexing_policy: Indexing Policy for the container.
            cosmos_container_properties: Container Properties for the container.
            cosmos_database_properties: Database Properties for the container.
            vector_search_fields: Vector Search Fields for the container.
            search_type: CosmosDB Search Type to be performed.
            metadata_key: Metadata key to use for data schema.
            create_container: Set to true if the container does not exist.
            full_text_search_enabled: Set to true if the full text search is enabled.
            table_alias: Alias for the table to use in the WHERE clause.
        """
        self._cosmos_client = cosmos_client
        self._database_name = database_name
        self._container_name = container_name
        self._embedding = embedding
        self._vector_embedding_policy = vector_embedding_policy
        self._full_text_policy = full_text_policy
        self._indexing_policy = indexing_policy
        self._cosmos_container_properties = cosmos_container_properties
        self._cosmos_database_properties = cosmos_database_properties
        self._vector_search_fields = vector_search_fields
        self._metadata_key = metadata_key
        self._create_container = create_container
        self._full_text_search_enabled = full_text_search_enabled
        self._search_type = search_type
        self._table_alias = table_alias

        if self._create_container:
            if (
                self._indexing_policy["vectorIndexes"] is None
                or len(self._indexing_policy["vectorIndexes"]) == 0
            ):
                raise ValueError(
                    "vectorIndexes cannot be null or empty in the indexing_policy."
                )
            if (
                self._vector_embedding_policy is None
                or len(vector_embedding_policy["vectorEmbeddings"]) == 0
            ):
                raise ValueError(
                    "vectorEmbeddings cannot be null "
                    "or empty in the vector_embedding_policy."
                )
            if self._cosmos_container_properties["partition_key"] is None:
                raise ValueError(
                    "partition_key cannot be null or empty for a container."
                )
            if self._full_text_search_enabled:
                if (
                    self._indexing_policy["fullTextIndexes"] is None
                    or len(self._indexing_policy["fullTextIndexes"]) == 0
                ):
                    raise ValueError(
                        "fullTextIndexes cannot be null or empty in the "
                        "indexing_policy if full text search is enabled."
                    )
                if (
                    self._full_text_policy is None
                    or len(self._full_text_policy["fullTextPaths"]) == 0
                ):
                    raise ValueError(
                        "fullTextPaths cannot be null or empty in the "
                        "full_text_policy if full text search is enabled."
                    )
        if self._vector_search_fields is None:
            raise ValueError(
                "vectorSearchFields cannot be null or empty in the vector_search_fields."  # noqa:E501
            )

        # Create the database if it already doesn't exist
        self._database = self._cosmos_client.create_database_if_not_exists(
            id=self._database_name,
            offer_throughput=self._cosmos_database_properties.get("offer_throughput"),
            session_token=self._cosmos_database_properties.get("session_token"),
            initial_headers=self._cosmos_database_properties.get("initial_headers"),
            etag=self._cosmos_database_properties.get("etag"),
            match_condition=self._cosmos_database_properties.get("match_condition"),
        )

        # Create the collection if it already doesn't exist
        self._container = self._database.create_container_if_not_exists(
            id=self._container_name,
            partition_key=self._cosmos_container_properties["partition_key"],
            indexing_policy=self._indexing_policy,
            default_ttl=self._cosmos_container_properties.get("default_ttl"),
            offer_throughput=self._cosmos_container_properties.get("offer_throughput"),
            unique_key_policy=self._cosmos_container_properties.get(
                "unique_key_policy"
            ),
            conflict_resolution_policy=self._cosmos_container_properties.get(
                "conflict_resolution_policy"
            ),
            analytical_storage_ttl=self._cosmos_container_properties.get(
                "analytical_storage_ttl"
            ),
            computed_properties=self._cosmos_container_properties.get(
                "computed_properties"
            ),
            etag=self._cosmos_container_properties.get("etag"),
            match_condition=self._cosmos_container_properties.get("match_condition"),
            session_token=self._cosmos_container_properties.get("session_token"),
            initial_headers=self._cosmos_container_properties.get("initial_headers"),
            vector_embedding_policy=self._vector_embedding_policy,
            full_text_policy=self._full_text_policy,
        )

    def add_texts(
        self,
        texts: Iterable[str],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Run more texts through the embeddings and add to the vectorstore.

        Args:
            texts: Iterable of strings to add to the vectorstore.
            metadatas: Optional list of metadatas associated with the texts.
            **kwargs: Additional keyword arguments to pass to the embedding method.

        Returns:
            List of ids from adding the texts into the vectorstore.
        """
        _metadatas = list(metadatas if metadatas is not None else ({} for _ in texts))

        return self._insert_texts(list(texts), _metadatas)

    def _insert_texts(
        self, texts: List[str], metadatas: List[Dict[str, Any]]
    ) -> List[str]:
        """Used to Load Documents into the collection.

        Args:
            texts: The list of documents strings to load
            metadatas: The list of metadata objects associated with each document

        Returns:
            List of ids from adding the texts into the vectorstore.
        """
        # If the texts is empty, throw an error
        if not texts:
            raise Exception("Texts can not be null or empty")

        # Embed and create the documents
        embeddings = self._embedding.embed_documents(texts)
        text_key = self._vector_search_fields["text_field"]
        embedding_key = self._vector_search_fields["embedding_field"]

        to_insert = [
            {
                "id": str(uuid.uuid4()),
                text_key: t,
                embedding_key: embedding,
                "metadata": m,
            }
            for t, m, embedding in zip(texts, metadatas, embeddings)
        ]
        # insert the documents in CosmosDB No Sql
        doc_ids: List[str] = []
        for item in to_insert:
            created_doc = self._container.create_item(item)
            doc_ids.append(created_doc["id"])
        return doc_ids

    @classmethod
    def _from_kwargs(
        cls,
        embedding: Embeddings,
        *,
        cosmos_client: CosmosClient,
        vector_embedding_policy: Dict[str, Any],
        indexing_policy: Dict[str, Any],
        cosmos_container_properties: Dict[str, Any],
        cosmos_database_properties: Dict[str, Any],
        vector_search_fields: Dict[str, Any],
        full_text_policy: Optional[Dict[str, Any]] = None,
        database_name: str = "vectorSearchDB",
        container_name: str = "vectorSearchContainer",
        metadata_key: str = "metadata",
        create_container: bool = True,
        full_text_search_enabled: bool = False,
        search_type: str = "vector",
        **kwargs: Any,
    ) -> AzureCosmosDBNoSqlVectorSearch:
        if kwargs:
            warnings.warn(
                "Method 'from_texts' of AzureCosmosDBNoSql vector "
                "store invoked with "
                f"unsupported arguments "
                f"({', '.join(sorted(kwargs))}), "
                "which will be ignored."
            )

        return cls(
            embedding=embedding,
            cosmos_client=cosmos_client,
            vector_embedding_policy=vector_embedding_policy,
            full_text_policy=full_text_policy,
            indexing_policy=indexing_policy,
            cosmos_container_properties=cosmos_container_properties,
            cosmos_database_properties=cosmos_database_properties,
            database_name=database_name,
            container_name=container_name,
            vector_search_fields=vector_search_fields,
            metadata_key=metadata_key,
            create_container=create_container,
            full_text_search_enabled=full_text_search_enabled,
            search_type=search_type,
        )

    @classmethod
    def from_texts(
        cls,
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> AzureCosmosDBNoSqlVectorSearch:
        """Create an AzureCosmosDBNoSqlVectorSearch vectorstore from raw texts.

        Args:
            texts: the texts to insert.
            embedding: the embedding function to use in the store.
            metadatas: metadata dicts for the texts.
            **kwargs: you can pass any argument that you would
                to :meth:`~add_texts` and/or to the 'AstraDB' constructor
                (see these methods for details). These arguments will be
                routed to the respective methods as they are.

        Returns:
            an `AzureCosmosDBNoSqlVectorSearch` vectorstore.
        """
        vectorstore = AzureCosmosDBNoSqlVectorSearch._from_kwargs(embedding, **kwargs)
        vectorstore.add_texts(
            texts=texts,
            metadatas=metadatas,
        )
        return vectorstore

    @classmethod
    def from_connection_string_and_aad(
        cls,
        connection_string: str,
        defaultAzureCredential: DefaultAzureCredential,
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> AzureCosmosDBNoSqlVectorSearch:
        """Initialize an AzureCosmosDBNoSqlVectorSearch vectorstore."""
        cosmos_client = CosmosClient(
            connection_string, defaultAzureCredential, user_agent=USER_AGENT
        )
        kwargs["cosmos_client"] = cosmos_client
        vectorstore = cls._from_kwargs(embedding, **kwargs)
        vectorstore.add_texts(
            texts=texts,
            metadatas=metadatas,
        )
        return vectorstore

    @classmethod
    def from_connection_string_and_key(
        cls,
        connection_string: str,
        key: str,
        texts: List[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> AzureCosmosDBNoSqlVectorSearch:
        """Initialize an AzureCosmosDBNoSqlVectorSearch vectorstore."""
        cosmos_client = CosmosClient(connection_string, key, user_agent=USER_AGENT)
        kwargs["cosmos_client"] = cosmos_client
        vectorstore = cls._from_kwargs(embedding, **kwargs)
        vectorstore.add_texts(
            texts=texts,
            metadatas=metadatas,
        )
        return vectorstore

    def delete(self, ids: Optional[List[str]] = None, **kwargs: Any) -> Optional[bool]:
        """Removes the documents based on ids."""
        if ids is None:
            raise ValueError("No document ids provided to delete.")

        for document_id in ids:
            self._container.delete_item(
                document_id, self._cosmos_container_properties["partition_key"]
            )  # noqa: E501
        return True

    def delete_document_by_id(self, document_id: Optional[str] = None) -> None:
        """Removes a Specific Document by id.

        Args:
            document_id: The document identifier
        """
        if document_id is None:
            raise ValueError("No document ids provided to delete.")
        self._container.delete_item(document_id, partition_key=document_id)

    def similarity_search(
        self,
        query: str,
        k: int = 4,
        with_embedding: bool = False,
        search_type: Optional[str] = "vector",
        offset_limit: Optional[str] = None,
        projection_mapping: Optional[Dict[str, Any]] = None,
        full_text_rank_filter: Optional[List[Dict[str, str]]] = None,
        where: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs most similar to query."""
        search_type = search_type or self._search_type

        docs_and_scores = self.similarity_search_with_score(
            query,
            k=k,
            with_embedding=with_embedding,
            search_type=search_type,
            offset_limit=offset_limit,
            projection_mapping=projection_mapping,
            full_text_rank_filter=full_text_rank_filter,
            where=where,
            kwargs=kwargs,
        )

        return [doc for doc, _ in docs_and_scores]

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        with_embedding: bool = False,
        search_type: Optional[str] = "vector",
        offset_limit: Optional[str] = None,
        full_text_rank_filter: Optional[List[Dict[str, str]]] = None,
        projection_mapping: Optional[Dict[str, Any]] = None,
        where: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """Run similarity search with distance."""
        docs_and_scores = []
        search_type = search_type or self._search_type
        if search_type == "vector":
            embeddings = self._embedding.embed_query(query)
            docs_and_scores = self.vector_search_with_score(
                search_type=search_type,
                embeddings=embeddings,
                k=k,
                with_embedding=with_embedding,
                offset_limit=offset_limit,
                projection_mapping=projection_mapping,
                where=where,
            )
        elif search_type == "full_text_search":
            docs_and_scores = self.full_text_search(
                k=k,
                search_type=search_type,
                offset_limit=offset_limit,
                projection_mapping=projection_mapping,
                where=where,
            )

        elif search_type == "full_text_ranking":
            docs_and_scores = self.full_text_ranking(
                k=k,
                search_type=search_type,
                offset_limit=offset_limit,
                full_text_rank_filter=full_text_rank_filter,
                projection_mapping=projection_mapping,
                where=where,
            )
        elif search_type == "hybrid":
            embeddings = self._embedding.embed_query(query)
            docs_and_scores = self.hybrid_search_with_score(
                search_type=search_type,
                embeddings=embeddings,
                k=k,
                with_embedding=with_embedding,
                offset_limit=offset_limit,
                full_text_rank_filter=full_text_rank_filter,
                projection_mapping=projection_mapping,
                where=where,
            )
        return docs_and_scores

    def max_marginal_relevance_search_by_vector(
        self,
        embedding: List[float],
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        search_type: str = "vector",
        with_embedding: bool = False,
        where: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance."""  # noqa:E501
        docs = self.vector_search_with_score(
            embeddings=embedding,
            k=fetch_k,
            search_type=search_type,
            with_embedding=with_embedding,
            where=where,
        )

        # Re-ranks the docs using MMR
        mmr_doc_indexes = maximal_marginal_relevance(
            np.array(embedding),
            [
                doc.metadata[self._vector_search_fields["embedding_field"]]
                for doc, _ in docs
            ],
            k=k,
            lambda_mult=lambda_mult,
        )

        mmr_docs = [docs[i][0] for i in mmr_doc_indexes]
        return mmr_docs

    def max_marginal_relevance_search(
        self,
        query: str,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        search_type: str = "vector",
        with_embedding: bool = False,
        where: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Document]:
        """Return docs selected using the maximal marginal relevance."""  # noqa:E501
        embeddings = self._embedding.embed_query(query)

        docs = self.max_marginal_relevance_search_by_vector(
            embeddings,
            k=k,
            fetch_k=fetch_k,
            lambda_mult=lambda_mult,
            search_type=search_type,
            with_embedding=with_embedding,
            where=where,
        )
        return docs

    def vector_search_with_score(
        self,
        search_type: str,
        embeddings: List[float],
        k: int = 4,
        with_embedding: bool = False,
        offset_limit: Optional[str] = None,
        *,
        projection_mapping: Optional[Dict[str, Any]] = None,
        where: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Tuple[Document, float]]:
        """Returns the most similar indexed documents to the embeddings."""  # noqa:E501
        query, parameters = self._construct_query(
            k=k,
            search_type=search_type,
            embeddings=embeddings,
            offset_limit=offset_limit,
            projection_mapping=projection_mapping,
            with_embedding=with_embedding,
            where=where,
        )

        return self._execute_query(
            query=query,
            search_type=search_type,
            parameters=parameters,
            with_embedding=with_embedding,
            projection_mapping=projection_mapping,
        )

    def full_text_search(
        self,
        search_type: str,
        k: int = 4,
        offset_limit: Optional[str] = None,
        *,
        projection_mapping: Optional[Dict[str, Any]] = None,
        where: Optional[str] = None,
    ) -> List[Tuple[Document, float]]:
        """Returns the documents based on the search text provided in the filters."""  # noqa:E501
        query, parameters = self._construct_query(
            k=k,
            search_type=search_type,
            offset_limit=offset_limit,
            projection_mapping=projection_mapping,
            where=where,
        )

        return self._execute_query(
            query=query,
            search_type=search_type,
            parameters=parameters,
            with_embedding=False,
            projection_mapping=projection_mapping,
        )

    def full_text_ranking(
        self,
        search_type: str,
        k: int = 4,
        offset_limit: Optional[str] = None,
        *,
        projection_mapping: Optional[Dict[str, Any]] = None,
        full_text_rank_filter: Optional[List[Dict[str, str]]] = None,
        where: Optional[str] = None,
    ) -> List[Tuple[Document, float]]:
        """Returns the documents based on the search text provided full text rank filters."""  # noqa:E501
        query, parameters = self._construct_query(
            k=k,
            search_type=search_type,
            offset_limit=offset_limit,
            projection_mapping=projection_mapping,
            full_text_rank_filter=full_text_rank_filter,
            where=where,
        )

        return self._execute_query(
            query=query,
            search_type=search_type,
            parameters=parameters,
            with_embedding=False,
            projection_mapping=projection_mapping,
        )

    def hybrid_search_with_score(
        self,
        search_type: str,
        embeddings: List[float],
        k: int = 4,
        with_embedding: bool = False,
        offset_limit: Optional[str] = None,
        *,
        projection_mapping: Optional[Dict[str, Any]] = None,
        full_text_rank_filter: Optional[List[Dict[str, str]]] = None,
        where: Optional[str] = None,
    ) -> List[Tuple[Document, float]]:
        """Returns the documents based on the embeddings and text provided full text rank filters."""  # noqa:E501
        query, parameters = self._construct_query(
            k=k,
            search_type=search_type,
            embeddings=embeddings,
            offset_limit=offset_limit,
            projection_mapping=projection_mapping,
            full_text_rank_filter=full_text_rank_filter,
            where=where,
        )
        return self._execute_query(
            query=query,
            search_type=search_type,
            parameters=parameters,
            with_embedding=with_embedding,
            projection_mapping=projection_mapping,
        )

    def _construct_query(
        self,
        k: int,
        search_type: str,
        embeddings: Optional[List[float]] = None,
        full_text_rank_filter: Optional[List[Dict[str, str]]] = None,
        offset_limit: Optional[str] = None,
        projection_mapping: Optional[Dict[str, Any]] = None,
        with_embedding: bool = False,
        where: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        if search_type == "full_text_ranking" or search_type == "hybrid":
            query = f"SELECT {'TOP ' + str(k) + ' ' if not offset_limit else ''}"
        else:
            query = f"""SELECT {'TOP @limit ' if not offset_limit else ''}"""
        query += self._generate_projection_fields(
            projection_mapping,
            search_type,
            embeddings,
            full_text_rank_filter,
            with_embedding,
        )
        table = self._table_alias
        query += f" FROM {table} "

        # Add where_clause if specified
        if where:
            query += f"WHERE {where}"

        # TODO: Update the code to use parameters once parametrized queries
        #  are allowed for these query functions
        if search_type == "full_text_ranking":
            if full_text_rank_filter is None:
                raise ValueError(
                    "full_text_rank_filter cannot be None for FULL_TEXT_RANK queries."
                )
            if len(full_text_rank_filter) == 1:
                query += f""" ORDER BY RANK FullTextScore({table}.{full_text_rank_filter[0]["search_field"]}, 
                [{", ".join(f"'{term}'" for term in full_text_rank_filter[0]["search_text"].split())}])"""  # noqa:E501
            else:
                rank_components = [
                    f"FullTextScore({table}.{search_item['search_field']}, ["
                    + ", ".join(
                        f"'{term}'" for term in search_item["search_text"].split()
                    )
                    + "])"
                    for search_item in full_text_rank_filter
                ]
                query = f" ORDER BY RANK RRF({', '.join(rank_components)})"
        elif search_type == "vector":
            query += " ORDER BY VectorDistance(c[@embeddingKey], @embeddings)"
        elif search_type == "hybrid":
            if full_text_rank_filter is None:
                raise ValueError(
                    "full_text_rank_filter cannot be None for HYBRID queries."
                )
            rank_components = [
                f"FullTextScore({table}.{search_item['search_field']}, ["
                + ", ".join(f"'{term}'" for term in search_item["search_text"].split())
                + "])"
                for search_item in full_text_rank_filter
            ]
            query += f""" ORDER BY RANK RRF({', '.join(rank_components)}, 
            VectorDistance({table}.{self._vector_search_fields["embedding_field"]}, {embeddings}))"""  # noqa:E501
        else:
            query += ""

        # Add limit_offset_clause if specified
        if offset_limit is not None:
            query += f""" {offset_limit}"""

        # TODO: Remove this if check once parametrized queries
        #  are allowed for these query functions
        parameters = []
        if search_type == "full_text_search" or search_type == "vector":
            parameters = self._build_parameters(
                k=k,
                search_type=search_type,
                embeddings=embeddings,
                projection_mapping=projection_mapping,
            )
        return query, parameters

    def _generate_projection_fields(
        self,
        projection_mapping: Optional[Dict[str, Any]],
        search_type: str,
        embeddings: Optional[List[float]] = None,
        full_text_rank_filter: Optional[List[Dict[str, str]]] = None,
        with_embedding: bool = False,
    ) -> str:
        # TODO: Remove the if check, lines 704-726 once parametrized queries
        #  are supported for these query functions.
        table = self._table_alias
        if search_type == "full_text_ranking" or search_type == "hybrid":
            if projection_mapping:
                projection = ", ".join(
                    f"{table}.{key} as {alias}"
                    for key, alias in projection_mapping.items()
                )
            elif full_text_rank_filter:
                projection = (
                    table
                    + ".id, "
                    + ", ".join(
                        f"{table}.{search_item['search_field']} "
                        f"as {search_item['search_field']}"
                        for search_item in full_text_rank_filter
                    )
                )
            else:
                projection = (
                    f"{table}.id, {table}.{self._vector_search_fields['text_field']} "
                )
                f"as text, {table}.{self._metadata_key} as metadata"
            if search_type == "hybrid":
                if with_embedding:
                    projection += f", {table}.{self._vector_search_fields['embedding_field']} as embedding"  # noqa:E501
                projection += (
                    f", VectorDistance({table}.{self._vector_search_fields['embedding_field']}, "  # noqa:E501
                    f"{embeddings}) as SimilarityScore"
                )
        else:
            if projection_mapping:
                projection = ", ".join(
                    f"{table}[@{key}] as {alias}"
                    for key, alias in projection_mapping.items()
                )
            elif full_text_rank_filter:
                projection = f"{table}.id" + ", ".join(
                    f"{table}.{search_item['search_field']} as {search_item['search_field']}"  # noqa: E501
                    for search_item in full_text_rank_filter
                )
            else:
                projection = f"{table}.id, {table}[@textKey] as text, {table}[@metadataKey] as metadata"  # noqa: E501

            if search_type == "vector":
                if with_embedding:
                    projection += f", {table}[@embeddingKey] as embedding"
                projection += (
                    f", VectorDistance({table}[@embeddingKey], "
                    "@embeddings) as SimilarityScore"
                )
        return projection

    def _build_parameters(
        self,
        k: int,
        search_type: str,
        embeddings: Optional[List[float]],
        projection_mapping: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        parameters: List[Dict[str, Any]] = [
            {"name": "@limit", "value": k},
        ]

        if projection_mapping:
            for key in projection_mapping.keys():
                parameters.append({"name": f"@{key}", "value": key})
        else:
            parameters.append(
                {"name": "@textKey", "value": self._vector_search_fields["text_field"]}
            )
            parameters.append({"name": "@metadataKey", "value": self._metadata_key})

        if search_type == "vector":
            parameters.append(
                {
                    "name": "@embeddingKey",
                    "value": self._vector_search_fields["embedding_field"],
                }
            )
            parameters.append({"name": "@embeddings", "value": embeddings})

        return parameters

    def _execute_query(
        self,
        query: str,
        search_type: str,
        parameters: List[Dict[str, Any]],
        with_embedding: bool,
        projection_mapping: Optional[Dict[str, Any]],
    ) -> List[Tuple[Document, float]]:
        docs_and_scores = []
        items = list(
            self._container.query_items(
                query=query, parameters=parameters, enable_cross_partition_query=True
            )
        )
        for item in items:
            text = item[self._vector_search_fields["text_field"]]
            metadata = item.pop(self._metadata_key, {})
            score = 0.0

            if projection_mapping:
                for key, alias in projection_mapping.items():
                    if key == self._vector_search_fields["text_field"]:
                        continue
                    metadata[alias] = item[alias]
            else:
                metadata["id"] = item["id"]

            if search_type == "vector" or search_type == "hybrid":
                score = item["SimilarityScore"]
                if with_embedding:
                    metadata[self._vector_search_fields["embedding_field"]] = item[
                        self._vector_search_fields["embedding_field"]
                    ]
            docs_and_scores.append(
                (Document(page_content=text, metadata=metadata), score)
            )
        return docs_and_scores

    def get_container(self) -> ContainerProxy:
        """Gets the container for the vector store."""
        return self._container

    def as_retriever(self, **kwargs: Any) -> AzureCosmosDBNoSqlVectorStoreRetriever:
        """Return AzureCosmosDBNoSqlVectorStoreRetriever initialized from this VectorStore.

        Args:
            search_type (Optional[str]): Overrides the type of search that
                the Retriever should perform. Defaults to `self._search_type`.
                Can be "vector", "hybrid", "full_text_ranking", "full_text_search".
            search_kwargs (Optional[Dict]): Keyword arguments to pass to the
                search function. Can include things like:
                    score_threshold: Minimum relevance threshold
                        for similarity_score_threshold
                    fetch_k: Amount of documents to pass to MMR algorithm (Default: 20)
                    lambda_mult: Diversity of results returned by MMR;
                        1 for minimum diversity and 0 for maximum. (Default: 0.5)
                    filter: Filter by document metadata
            **kwargs: Additional keyword arguments to pass to the

        Returns:
            AzureCosmosDBNoSqlVectorStoreRetriever: Retriever class for VectorStore.
        """  # noqa:E501
        search_type = kwargs.get("search_type", self._search_type)
        kwargs["search_type"] = search_type

        tags = kwargs.pop("tags", None) or []
        tags.extend(self._get_retriever_tags())
        return AzureCosmosDBNoSqlVectorStoreRetriever(
            vectorstore=self, **kwargs, tags=tags
        )


class AzureCosmosDBNoSqlVectorStoreRetriever(VectorStoreRetriever):
    """Retriever that uses `Azure CosmosDB No Sql Search`."""

    vectorstore: AzureCosmosDBNoSqlVectorSearch
    """Azure Search instance used to find similar documents."""
    search_type: str = "vector"
    """Type of search to perform. Options are "vector", 
    "hybrid", "full_text_ranking", "full_text_search"."""
    k: int = 4
    """Number of documents to return."""
    search_kwargs: dict = {}
    """Search params.
        score_threshold: Minimum relevance threshold
            for similarity_score_threshold
        fetch_k: Amount of documents to pass to MMR algorithm (Default: 20)
        lambda_mult: Diversity of results returned by MMR;
            1 for minimum diversity and 0 for maximum. (Default: 0.5)
        filter: Filter by document metadata
    """

    allowed_search_types: ClassVar[Collection[str]] = (
        "vector",
        "hybrid",
        "full_text_ranking",
        "full_text_search",
    )

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
    )

    @model_validator(mode="before")
    @classmethod
    def validate_search_type(cls, values: Dict) -> Any:
        """Validate search type."""
        if "search_type" in values:
            search_type = values["search_type"]
            if search_type not in cls.allowed_search_types:
                raise ValueError(
                    f"search_type of {search_type} not allowed. Valid values are: "
                    f"{cls.allowed_search_types}"
                )
        return values

    def _get_relevant_documents(
        self,
        query: str,
        run_manager: CallbackManagerForRetrieverRun,
        **kwargs: Any,
    ) -> List[Document]:
        params = {**self.search_kwargs, **kwargs}

        if self.search_type == "vector":
            docs = self.vectorstore.similarity_search(query, k=self.k, **params)
        elif self.search_type == "hybrid":
            docs = self.vectorstore.similarity_search(
                query, k=self.k, search_type="hybrid", **params
            )
        elif self.search_type == "full_text_ranking":
            docs = self.vectorstore.similarity_search(
                query, k=self.k, search_type="full_text_ranking", **params
            )
        elif self.search_type == "full_text_search":
            docs = self.vectorstore.similarity_search(
                query, k=self.k, search_type="full_text_search", **params
            )
        else:
            raise ValueError(f"Query type of {self.search_type} is not allowed.")
        return docs
