"""Generate informalizations with an LLM, one dependency layer at a time.

Each declaration's prompt includes the informal descriptions of its already
informalized dependencies, so layers are processed in dependency order and each
layer's results are made available to the next.
"""

import asyncio
import logging
from dataclasses import dataclass

from rich.progress import Progress, TaskID
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from lean_explore.extract.dependency_layers import parse_dependencies
from lean_explore.extract.progress import create_progress
from lean_explore.models import Declaration
from lean_explore.util import OpenRouterClient

logger = logging.getLogger(__name__)

MAX_PROMPT_DEPENDENCIES = 20
"""Only the first this-many dependencies are considered for prompt context."""

MAX_DEPENDENCY_DESCRIPTION_LENGTH = 256
"""Dependency descriptions longer than this are truncated with an ellipsis."""

InformalizationCache = dict[tuple[str, str], str]
"""Map of (declaration name, source text) to a previously generated description."""


# --- Data Classes ---


@dataclass
class InformalizationResult:
    """Result of processing a single declaration."""

    declaration_id: int
    declaration_name: str
    informalization: str | None


@dataclass
class DeclarationData:
    """Plain data extracted from Declaration ORM object for async processing."""

    id: int
    name: str
    source_text: str
    docstring: str | None
    dependencies: str | None
    informalization: str | None

    @classmethod
    def from_declaration(cls, declaration: Declaration) -> "DeclarationData":
        """Copy the fields needed for informalization out of an ORM object.

        Args:
            declaration: Declaration ORM object.

        Returns:
            Detached plain-data copy, safe to use from concurrent tasks.
        """
        return cls(
            id=declaration.id,
            name=declaration.name,
            source_text=declaration.source_text,
            docstring=declaration.docstring,
            dependencies=declaration.dependencies,
            informalization=declaration.informalization,
        )


# --- Processing Functions ---


def format_dependencies(
    dependencies: str | list[str] | None, informalizations_by_name: dict[str, str]
) -> str:
    """Format known dependency descriptions as prompt context.

    Args:
        dependencies: Dependency names as JSON string, list, or None.
        informalizations_by_name: Map of declaration names to informalizations.

    Returns:
        "Dependencies:" followed by one line per described dependency, or ""
        when none of the first MAX_PROMPT_DEPENDENCIES are described.
    """
    lines = []
    for name in parse_dependencies(dependencies)[:MAX_PROMPT_DEPENDENCIES]:
        if name not in informalizations_by_name:
            continue
        description = informalizations_by_name[name]
        if len(description) > MAX_DEPENDENCY_DESCRIPTION_LENGTH:
            description = description[: MAX_DEPENDENCY_DESCRIPTION_LENGTH - 3] + "..."
        lines.append(f"- {name}: {description}")
    return "Dependencies:\n" + "\n".join(lines) if lines else ""


def build_prompt(
    declaration_data: DeclarationData,
    prompt_template: str,
    informalizations_by_name: dict[str, str],
) -> str:
    """Fill the prompt template for one declaration.

    Args:
        declaration_data: Declaration to describe.
        prompt_template: Template with name/source_text/docstring/dependencies.
        informalizations_by_name: Map of declaration names to informalizations.

    Returns:
        The formatted prompt.
    """
    return prompt_template.format(
        name=declaration_data.name,
        source_text=declaration_data.source_text,
        docstring=declaration_data.docstring or "No docstring available",
        dependencies=format_dependencies(
            declaration_data.dependencies, informalizations_by_name
        ),
    )


async def process_one_declaration(
    *,
    declaration_data: DeclarationData,
    client: OpenRouterClient,
    model: str,
    prompt_template: str,
    informalizations_by_name: dict[str, str],
    cache: InformalizationCache,
    semaphore: asyncio.Semaphore,
) -> InformalizationResult:
    """Process a single declaration and generate its informalization.

    Args:
        declaration_data: Plain data extracted from Declaration ORM object
        client: OpenRouter client
        model: Model name to use
        prompt_template: Prompt template string
        informalizations_by_name: Map of declaration names to informalizations
        cache: Map of (name, source_text) to cached informalizations
        semaphore: Concurrency control semaphore

    Returns:
        InformalizationResult with declaration info and generated informalization
        (None if the declaration was already informalized or the LLM returned
        an empty response).
    """

    def result(informalization: str | None) -> InformalizationResult:
        return InformalizationResult(
            declaration_id=declaration_data.id,
            declaration_name=declaration_data.name,
            informalization=informalization,
        )

    if declaration_data.informalization is not None:
        return result(None)

    cache_key = (declaration_data.name, declaration_data.source_text)
    if cache_key in cache:
        return result(cache[cache_key])

    async with semaphore:
        prompt = build_prompt(
            declaration_data, prompt_template, informalizations_by_name
        )
        response = await client.generate(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )

    if response.choices and response.choices[0].message.content:
        return result(response.choices[0].message.content.strip())

    logger.warning("Empty response for declaration %s", declaration_data.name)
    return result(None)


@dataclass
class LayerProgress:
    """Total and per-commit-batch progress bars for layer processing."""

    progress: Progress
    total_task: TaskID
    batch_task: TaskID

    def advance(self) -> None:
        """Record one processed declaration."""
        self.progress.update(self.total_task, advance=1)
        self.progress.update(self.batch_task, advance=1)

    def reset_batch(self) -> None:
        """Restart the batch bar after a commit."""
        self.progress.reset(self.batch_task)


@dataclass
class InformalizationRun:
    """Shared state for generating informalizations with an LLM."""

    session: AsyncSession
    client: OpenRouterClient
    model: str
    prompt_template: str
    informalizations_by_name: dict[str, str]
    cache: InformalizationCache
    semaphore: asyncio.Semaphore
    commit_batch_size: int

    async def informalize(self, data: DeclarationData) -> InformalizationResult:
        """Generate the informalization of one declaration."""
        return await process_one_declaration(
            declaration_data=data,
            client=self.client,
            model=self.model,
            prompt_template=self.prompt_template,
            informalizations_by_name=self.informalizations_by_name,
            cache=self.cache,
            semaphore=self.semaphore,
        )

    async def commit(self, pending_updates: list[dict]) -> None:
        """Write a batch of informalization updates and commit."""
        await self.session.execute(update(Declaration), pending_updates)
        await self.session.commit()
        logger.info("Committed batch of %d updates", len(pending_updates))


async def process_layer(
    run: InformalizationRun,
    layer: list[Declaration],
    layer_progress: LayerProgress,
) -> int:
    """Process a single dependency layer concurrently.

    Args:
        run: Shared informalization state.
        layer: List of declarations in this layer
        layer_progress: Progress bars to advance.

    Returns:
        Number of declarations informalized in this layer
    """
    processed = 0
    pending_updates: list[dict] = []

    # Copy ORM data before starting tasks to avoid concurrent session access
    tasks = [
        asyncio.create_task(run.informalize(DeclarationData.from_declaration(d)))
        for d in layer
    ]

    for completed in asyncio.as_completed(tasks):
        result = await completed
        if result.informalization:
            pending_updates.append(
                {"id": result.declaration_id, "informalization": result.informalization}
            )
            run.informalizations_by_name[result.declaration_name] = (
                result.informalization
            )
            processed += 1

        layer_progress.advance()
        if len(pending_updates) >= run.commit_batch_size:
            await run.commit(pending_updates)
            pending_updates.clear()
            layer_progress.reset_batch()

    if pending_updates:
        await run.commit(pending_updates)
        layer_progress.reset_batch()

    return processed


async def process_layers(
    run: InformalizationRun, layers: list[list[Declaration]]
) -> int:
    """Process declarations layer by layer with progress tracking.

    Args:
        run: Shared informalization state.
        layers: List of dependency layers to process

    Returns:
        Number of declarations processed
    """
    total = sum(len(layer) for layer in layers)
    processed = 0

    with create_progress() as progress:
        layer_progress = LayerProgress(
            progress=progress,
            total_task=progress.add_task(f"[cyan]Total ({total:,})", total=total),
            batch_task=progress.add_task(
                f"[green]Batch ({run.commit_batch_size:,})",
                total=run.commit_batch_size,
            ),
        )
        for layer_num, layer in enumerate(layers, 1):
            logger.info(
                "Processing layer %d/%d (%d declarations)",
                layer_num,
                len(layers),
                len(layer),
            )
            layer_processed = await process_layer(run, layer, layer_progress)
            processed += layer_processed
            logger.info(
                "Completed layer %d: %d/%d declarations informalized",
                layer_num,
                layer_processed,
                len(layer),
            )

    return processed
