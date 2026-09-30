"""Stable control identifiers without importing a GPU or model framework."""

DEFAULT_DEPTH_MODEL = "depth_anything_v2_small"
DAD_DEPTH_MODEL = "distill_any_depth_small"
DEPTH_MODEL_IDS = (DEFAULT_DEPTH_MODEL, DAD_DEPTH_MODEL)
DEPTH_MODEL_LABELS = {
    DEFAULT_DEPTH_MODEL: "DAv2 Small",
    DAD_DEPTH_MODEL: "DAD Small",
}


def validate_depth_model(value):
    if not isinstance(value, str) or value not in DEPTH_MODEL_IDS:
        raise ValueError("Depth model must be DAv2 Small or DAD Small")
    return value
