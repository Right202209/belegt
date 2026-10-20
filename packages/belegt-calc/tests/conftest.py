# Spec: 05 §9
from __future__ import annotations

import pytest

from belegt_calc import load_factor_set


@pytest.fixture(scope="session")
def fs_example():
    return load_factor_set("fs:belegt-example@2026.10")


@pytest.fixture(scope="session")
def fs_cbam():
    return load_factor_set("fs:belegt-example-cbam@2026.10")


@pytest.fixture(scope="session")
def fs_price():
    return load_factor_set("fs:belegt-example-cbam-price@2026Q3")
