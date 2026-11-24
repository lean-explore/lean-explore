# Qwen reranker scoring

The Qwen3 reranker scores each query and declaration at the next assistant token. The client wraps the task instruction, query and document in a conversation and computes a softmax over the `no` and `yes` logits. The returned score is the normalized `yes` score for that pair. It is not a calibrated probability that a Lean declaration solves the user's task.

The previous client used `false` and `true` logits after a flat input. This differed from the scoring contract documented in the [Qwen3-Reranker model card](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B). The corrected client uses the documented conversation structure and answer tokens with a short, task-independent system instruction. It does not reproduce Qwen's published evaluation protocol.

`max_length` includes the conversation prefix and assistant suffix. The client reserves space for both before truncating the query/document body, then left-pads the batch. This keeps the model's answer position intact even when the content exceeds the context budget. Budgets too small to hold the wrapper and any content fail before model loading.

Only the final token's logits are requested from the causal language model. Earlier token distributions are unused by this scoring method and can consume substantial memory. This optimization leaves the forward context unchanged.

The search engine still uses a 256-token reranker limit and its existing candidate selection and final score weights. The full-collection benchmark compares the corrected client on the same candidate pools and records context-length experiments separately. Those experiments do not establish production latency or relevance on traffic without judgments.

Regression tests cover answer-token selection, input order, batching, truncation, left padding and invalid context budgets. A separate offline check with cached model weights compares final-token logits with the full-logit calculation and checks that a relevant declaration outranks unrelated declarations.
