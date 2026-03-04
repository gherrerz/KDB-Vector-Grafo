"""
Patch para resolver incompatibilidad de chromadb con Python 3.14 y pydantic v1
"""
import logging
import warnings
from typing import Any


LOGGER = logging.getLogger(__name__)

# Suppress warnings
warnings.filterwarnings("ignore", category=UserWarning)

# Monkey-patch to handle pydantic v1 compatibility issues with Python 3.14
try:
    from pydantic import v1 as pydantic_v1

    # Store the original ModelField.infer method
    original_infer = pydantic_v1.fields.ModelField.infer

    def patched_infer(
        cls,
        name: str,
        value: Any,
        annotation: Any,
        class_validators: dict[str, Any] | None = None,
        config: Any = None,
    ) -> Any:
        """Patched version that handles missing type hints for >=Python 3.13"""
        try:
            return original_infer(
                name,
                value,
                annotation,
                class_validators,
                config,
            )
        except pydantic_v1.errors.ConfigError as exc:
            if "unable to infer type" in str(exc):
                # Skip inference for problematic fields, use Any type
                return pydantic_v1.fields.ModelField(
                    name=name,
                    type_=Any,
                    class_validators={},
                    model_config=config or {},
                )
            raise

    # Apply the patch
    pydantic_v1.fields.ModelField.infer = classmethod(patched_infer)
except ImportError:
    LOGGER.warning("No se pudo importar pydantic.v1; patch omitido.")
except AttributeError as exc:
    LOGGER.warning("No se pudo aplicar patch de pydantic: %s", exc)
