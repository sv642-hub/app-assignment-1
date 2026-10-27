"""Measure request-processing stages without HTTP transport."""

from time import perf_counter

from app_assignment_1.api import PredictRequest, PredictResponse
from app_assignment_1.model import TextGenerator

TEXT = "What is a bank? Answer in one short sentence."

print("Loading model once...")
generator = TextGenerator(
    model_name="HuggingFaceTB/SmolLM2-135M-Instruct",
    max_new_tokens=64,
)

print("Warming up...")
generator.generate(TEXT)

start = perf_counter()
request = PredictRequest.model_validate({"text": TEXT})
validation_seconds = perf_counter() - start

start = perf_counter()
answer = generator.generate(request.text)
generation_seconds = perf_counter() - start

start = perf_counter()
response = PredictResponse(
    prediction=answer,
    input_length=len(request.text),
)
response.model_dump_json()
serialization_seconds = perf_counter() - start

print("\nSingle-request stage measurements:")
print(f"Input validation: {validation_seconds * 1000:.3f} ms")
print(f"Model generation: {generation_seconds * 1000:.3f} ms")
print(f"Response construction + JSON: {serialization_seconds * 1000:.3f} ms")
print("\nAnswer:", answer)
