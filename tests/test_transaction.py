from fastapi.testclient import TestClient

from fastapi_batch_api.app import Transaction

def test_transaction_of_get_requests(test_client:TestClient):

    response = test_client.post(
        "/",
        json={
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "request": {
                        "method": "GET",
                        "url": "Patient/1"
                    }
                },
                {
                    "request": {
                        "method": "GET",
                        "url": "Patient/2"
                    }
                }
            ]
        })
    assert response.status_code == 200, response.text
    bundle = response.json()

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "transaction-response"

    assert len(bundle["entry"]) == 2, bundle["entry"]

    entry_0 = bundle["entry"][0]
    assert entry_0["response"]["status"] == "200"
    assert entry_0["resource"]["resourceType"] == "Patient"
    assert entry_0["resource"]["id"] == "1"

    entry_1 = bundle["entry"][1]
    assert entry_1["response"]["status"] == "200"
    assert entry_1["resource"]["resourceType"] == "Patient"
    assert entry_1["resource"]["id"] == "2"

    assert len(Transaction.TRANSACTION_LOG) == 1, "All requests should be in the same transaction"

    assert len(Transaction.TRANSACTION_LOG[0]) == 4, "expected start, get, get, commit"
