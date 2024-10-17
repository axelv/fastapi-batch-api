import pytest
from fastapi.testclient import TestClient

from fastapi_batch_api.app import Transaction, app


@pytest.fixture
def test_client():
    Transaction.TRANSACTION_LOG = []
    return TestClient(app)
