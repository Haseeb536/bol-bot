#!/usr/bin/env python3
import hashlib
import re

# Must match bol_checkout CREATE_PAYMENT_QUERY exactly
QUERY = """
mutation CheckoutCreatePaymentMutation(
  $createPaymentInput: PaymentCreationRequest!
  $requestSource: RequestSource
) {
  paymentExecutions {
    createPayment(
      createPaymentInput: $createPaymentInput
      requestSource: $requestSource
    ) {
      __typename
      ... on Payment {
        id
        status
        paymentFollowUpAction {
          __typename
          idealActionDetails { redirectUrl }
        }
      }
      ... on PaymentExecutionProblem { errorCode }
    }
  }
}
"""

def apq_hash(query: str) -> str:
    norm = re.sub(r"\s+", " ", query.strip())
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()

print(apq_hash(QUERY))

# Minified one-liner variant
mini = re.sub(r"\s+", " ", QUERY.replace("\n", " ").strip())
print("mini", apq_hash(mini))
