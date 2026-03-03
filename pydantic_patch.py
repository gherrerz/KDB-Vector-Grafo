"""
Patch para resolver incompatibilidad de chromadb con Python 3.14 y pydantic v1
"""
import sys
import warnings

# Suppress warnings
warnings.filterwarnings('ignore', category=UserWarning)

# Monkey-patch to handle pydantic v1 compatibility issues with Python 3.14
try:
    from pydantic import v1 as pydantic_v1
    
    # Store the original ModelField.infer method
    original_infer = pydantic_v1.fields.ModelField.infer
    
    def patched_infer(cls, name, value, annotation, class_validators=None, config=None):
        """Patched version that handles missing type hints for >=Python 3.13"""
        try:
            return original_infer(name, value, annotation, class_validators, config)
        except pydantic_v1.errors.ConfigError as e:
            if "unable to infer type" in str(e):
                # Skip inference for problematic fields, use Any type
                from typing import Any
                return pydantic_v1.fields.ModelField(
                    name=name,
                    type_=Any,
                    class_validators={},
                    model_config=config or {}
                )
            raise
    
    # Apply the patch
    pydantic_v1.fields.ModelField.infer = classmethod(patched_infer)
except Exception:
    pass
