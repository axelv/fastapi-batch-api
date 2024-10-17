from fastapi.testclient import TestClient
from fastapi_batch_api.app import Transaction

def test_batch_of_get_requests(test_client:TestClient):

    response = test_client.post(
        "/",
        json={
            "resourceType": "Bundle",
            "type": "batch",
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
    assert bundle["type"] == "batch-response"

    assert len(bundle["entry"]) == 2, bundle["entry"]

    entry_0 = bundle["entry"][0]
    assert entry_0["response"]["status"] == "200"
    assert entry_0["resource"]["resourceType"] == "Patient"
    assert entry_0["resource"]["id"] == "1"

    entry_1 = bundle["entry"][1]
    assert entry_1["response"]["status"] == "200"
    assert entry_1["resource"]["resourceType"] == "Patient"
    assert entry_1["resource"]["id"] == "2"

    assert len(Transaction.TRANSACTION_LOG) == 3

    assert len(Transaction.TRANSACTION_LOG[0]) == 3
    assert len(Transaction.TRANSACTION_LOG[1]) == 3
    assert len(Transaction.TRANSACTION_LOG[2]) == 2

def test_batch_of_get_requests_and_failing_update(test_client:TestClient):

    response = test_client.post(
        "/",
        json={
            "resourceType": "Bundle",
            "type": "batch",
            "entry": [
                {
                    "request": {
                        "method": "GET",
                        "url": "Patient/1"
                    }
                },
                {
                    "request": {
                        "method": "PUT",
                        "url": "Error/1",
                        "resource": {
                            "resourceType": "Patient",
                            "id": "1",
                            "active": True
                        }
                    }
                }
            ]
        })
    assert response.status_code == 200, response.text
    bundle = response.json()

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "batch-response"

    assert len(bundle["entry"]) == 2, bundle["entry"]

    entry_0 = bundle["entry"][0]
    assert entry_0["response"]["status"] == "200"
    assert entry_0["resource"]["resourceType"] == "Patient"
    assert entry_0["resource"]["id"] == "1"

    entry_1 = bundle["entry"][1]
    assert entry_1["response"]["status"] == "501"
    assert "resource" not in entry_1

    assert len(Transaction.TRANSACTION_LOG) == 3

    assert len(Transaction.TRANSACTION_LOG[0]) == 3
    assert len(Transaction.TRANSACTION_LOG[1]) == 3
    assert len(Transaction.TRANSACTION_LOG[2]) == 2
