# Needle HTTP Backend Contract

Status: draft contract for issue #6. The registry metadata exists so the
runtime can grow an HTTP implementation without changing Needle's product
ontology.

## Backend Object

`needle/registry_data/backends/e24z/code-pruner-http.yaml` describes an HTTP JSON backend that still
runs behind the local Needle manager. Host adapters continue to call Needle;
Needle may then call a user-configured HTTP endpoint.

The endpoint is never implicit. A runtime implementation must require
`NEEDLE_HTTP_PRUNER_URL` before sending any tool output over the network.

## Request

The manager sends a `POST` request to `/v1/prune` with
`Content-Type: application/json`.

```json
{
  "text": "tool output to prune",
  "context_focus_question": "optional user/model intent",
  "capability": "swe-pruner/reference",
  "metadata": {
    "tool": "read",
    "artifact_kind": "file_text"
  }
}
```

Only `text` is required by the transport contract. Implementations should pass
`context_focus_question` when the host binding supplies it.

## Response

Successful responses use HTTP `200` with `Content-Type: application/json`.

```json
{
  "text": "possibly pruned tool output",
  "changed": true,
  "reason": "visible_prune",
  "accounting": {
    "original_chars": 12000,
    "returned_chars": 7000
  }
}
```

Only response `text` is required. Missing optional fields must not prevent
Needle from returning the response text.

## Failure And Privacy

Failure behavior is fail-open: if the endpoint is missing, times out, returns a
non-200 status, returns invalid JSON, or omits `text`, Needle must return the
original tool output unchanged.

Remote HTTP endpoints receive unredacted tool output and focus hints. Users
must opt into this by configuring `NEEDLE_HTTP_PRUNER_URL`; packages using the
HTTP backend must surface that privacy posture in their package card and claim
card before public release.
