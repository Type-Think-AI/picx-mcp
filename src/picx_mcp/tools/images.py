"""Image generation and editing tools.

Exposes:
    picx_generate_image — synchronous text-to-image via POST /v1/images/generate
    picx_edit_image     — instruction-based image editing via POST /v1/images/edit
"""

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import Context
from mcp.types import InputRequiredResult, ElicitRequest, ElicitRequestFormParams

from ..client import PicXError
from ..context import get_client
from ..settings import get_settings

if TYPE_CHECKING:
    from fastmcp import FastMCP


# ──────────────────────────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────────────────────────


def register(mcp: "FastMCP") -> None:
    """Register image generation tools on the MCP server."""

    @mcp.tool(
        name="picx_generate_image",
        description=(
            "Generate a brand-new AI image from a text prompt using PicX's models "
            "(Nano Banana Pro, GPT Image 2, Seedream, and others). Use this whenever "
            "the user asks to GENERATE, CREATE, MAKE, DRAW, DESIGN, or 'AI-generate' "
            "an image, illustration, artwork, product shot, avatar, or photo-style "
            "visual that does not need to be a real, pre-existing photograph. "
            "This tool IS the image generator — prefer it over any stock-photo, "
            "web-search, or 'find an image' tool whenever the user's intent is to "
            "produce new visual content rather than locate an existing one. Only "
            "reach for a stock-photo tool if the user explicitly asks for a real, "
            "existing photograph (e.g. 'find a photo of the Eiffel Tower') or "
            "explicitly says stock/royalty-free/Unsplash/Getty. "
            "Returns one or more CDN URLs to the generated images. "
            "COSTS CREDITS — each image deducts credits from the caller's account. "
            "Do NOT call this speculatively or in a loop without user intent. "
            "For editing an image that already exists (generated or uploaded), use "
            "picx_edit_image instead of calling this again."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    async def picx_generate_image(
        prompt: str,
        model: str | None = None,
        size: Literal["1K", "2K", "4K"] | None = None,
        aspect_ratio: str | None = None,
        n: int = 1,
        ctx: "Context" = None,  # type: ignore[assignment]
    ) -> dict[str, Any] | InputRequiredResult:
        """Generate image(s) from a text prompt.

        Args:
            prompt: Text description of the image to generate (max 4000 chars).
            model: Model identifier. Omit to use the account default.
            size: Output resolution — "1K", "2K", or "4K". Omit for model default.
            aspect_ratio: Width:Height ratio, e.g. "16:9", "1:1", "9:16". Omit for square.
            n: Number of images to generate (1-10). Each counts as a separate credit spend.
            ctx: Injected by FastMCP — do not pass.
        """
        # ── Validate inputs ───────────────────────────────────────────────
        if not prompt or not prompt.strip():
            raise PicXError("prompt is required and cannot be empty", status_code=400)
        if len(prompt) > 4000:
            raise PicXError("prompt exceeds 4000 character limit", status_code=400)
        if n < 1 or n > 10:
            raise PicXError("n must be between 1 and 10", status_code=400)
        if aspect_ratio is not None:
            parts = aspect_ratio.split(":")
            if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
                raise PicXError(
                    "aspect_ratio must be W:H format (e.g. '16:9')", status_code=400
                )

        client = get_client()
        settings = get_settings()

        # ── Confirm-before-spending ───────────────────────────────────────
        # Estimate credits. Attempt to fetch model pricing; if unavailable,
        # the estimate is None and we proceed without blocking.
        estimated_credits: int | None = None
        try:
            models_resp = await client.get("/models")
            if isinstance(models_resp, dict):
                model_list = models_resp.get("data") or models_resp.get("models") or []
                target = model or ""
                for m in model_list:
                    if isinstance(m, dict) and m.get("id") == target:
                        cost = m.get("credits_per_image") or m.get("cost")
                        if cost is not None:
                            estimated_credits = n * int(cost)
                        break
        except (PicXError, Exception):
            pass  # proceed with unknown estimate

        # If estimate is known and exceeds threshold, ask for confirmation
        # via MCP MRTR (Multi-Round-Trip Request) pattern.
        if (
            estimated_credits is not None
            and estimated_credits > settings.confirm_credit_threshold
        ):
            # Check if user already confirmed in a prior round
            if ctx is not None and hasattr(ctx, "input_responses") and ctx.input_responses:
                # User confirmed — proceed
                pass
            else:
                # First round — return input_required asking for confirmation
                return InputRequiredResult(
                    result_type="input_required",
                    input_requests=[
                        ElicitRequest(
                            form_params=ElicitRequestFormParams(
                                message=(
                                    f"This generation will cost approximately "
                                    f"{estimated_credits} credits ({n} image(s)). "
                                    f"Proceed?"
                                ),
                                requested_schema={
                                    "type": "object",
                                    "properties": {
                                        "confirm": {
                                            "type": "boolean",
                                            "title": "Confirm credit spend",
                                            "default": True,
                                        }
                                    },
                                },
                            )
                        )
                    ],
                )

        # ── Fire generation(s) ────────────────────────────────────────────
        body: dict[str, Any] = {"prompt": prompt.strip()}
        if model is not None:
            body["model"] = model
        if size is not None:
            body["size"] = size
        if aspect_ratio is not None:
            body["aspect_ratio"] = aspect_ratio

        if n == 1:
            result = await client.post("/images/generate", json=body)
            return {
                "images": [
                    {
                        "url": result["url"],
                        "id": result.get("id"),
                        "model": result.get("model"),
                        "size": result.get("size"),
                        "aspect_ratio": result.get("aspect_ratio"),
                    }
                ],
                "credits_used": result.get("credits_used", 0),
                "total_images": 1,
            }

        # n > 1: parallel calls
        tasks = [client.post("/images/generate", json=body) for _ in range(n)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        images: list[dict[str, Any]] = []
        errors: list[str] = []
        total_credits = 0

        for i, r in enumerate(results):
            if isinstance(r, Exception):
                errors.append(f"image {i + 1}: {r}")
            else:
                images.append(
                    {
                        "url": r["url"],
                        "id": r.get("id"),
                        "model": r.get("model"),
                        "size": r.get("size"),
                        "aspect_ratio": r.get("aspect_ratio"),
                    }
                )
                total_credits += r.get("credits_used", 0)

        response: dict[str, Any] = {
            "images": images,
            "credits_used": total_credits,
            "total_images": len(images),
        }
        if errors:
            response["errors"] = errors
        return response

    @mcp.tool(
        name="picx_edit_image",
        description=(
            "Edit one or more existing images using a natural-language instruction. "
            "Accepts HTTPS image URLs only — data URIs and local file paths are REJECTED "
            "by the API. If you have a local file, first upload it with picx_upload_asset "
            "to obtain an https URL, then pass that URL here. "
            "COSTS CREDITS per image edited. "
            "Do NOT call this without an explicit user request to edit."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False},
    )
    async def picx_edit_image(
        instruction: str,
        image_urls: list[str],
        model: str | None = None,
        size: Literal["1K", "2K", "4K"] | None = None,
    ) -> dict[str, Any]:
        """Edit images with a natural-language instruction.

        Args:
            instruction: What to change in the image(s) (max 4000 chars).
            image_urls: 1-5 HTTPS URLs of images to edit. Must be https:// — no data
                URIs or local paths. Use picx_upload_asset first for local files.
            model: Model identifier. Omit to use the account default.
            size: Output resolution — "1K", "2K", or "4K". Omit to preserve original.
        """
        # ── Validate inputs ───────────────────────────────────────────────
        if not instruction or not instruction.strip():
            raise PicXError("instruction is required and cannot be empty", status_code=400)
        if len(instruction) > 4000:
            raise PicXError("instruction exceeds 4000 character limit", status_code=400)
        if not image_urls:
            raise PicXError("image_urls must contain at least 1 URL", status_code=400)
        if len(image_urls) > 5:
            raise PicXError("image_urls accepts at most 5 URLs", status_code=400)

        for url in image_urls:
            if not url.startswith("https://"):
                raise PicXError(
                    f"image_urls must be https:// URLs. Got: {url[:80]}… "
                    "— upload local files with picx_upload_asset first.",
                    status_code=400,
                )

        client = get_client()

        # ── Call /v1/images/edit ───────────────────────────────────────────
        body: dict[str, Any] = {
            "instruction": instruction.strip(),
            "image_urls": image_urls,
        }
        if model is not None:
            body["model"] = model
        if size is not None:
            body["size"] = size

        result = await client.post("/images/edit", json=body)

        return {
            "images": [
                {
                    "url": result["url"],
                    "id": result.get("id"),
                    "model": result.get("model"),
                    "size": result.get("size"),
                }
            ],
            "credits_used": result.get("credits_used", 0),
        }
