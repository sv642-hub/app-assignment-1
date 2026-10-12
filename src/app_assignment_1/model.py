"""Local text generation using SmolLM2."""

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_NAME = "HuggingFaceTB/SmolLM2-135M-Instruct"
MAX_NEW_TOKENS = 64


class TextGenerator:
    def __init__(
        self,
        model_name: str = MODEL_NAME,
        max_new_tokens: int = MAX_NEW_TOKENS,
    ) -> None:
        self.max_new_tokens = max_new_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForCausalLM.from_pretrained(model_name)
        self.model.eval()

    def generate(self, text: str) -> str:
        messages = [
            {
                "role": "system",
                "content": "You are a helpful assistant. Reply briefly in English.",
            },
            {"role": "user", "content": text},
        ]

        prompt = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self.tokenizer(
            prompt,
            return_tensors="pt",
            add_special_tokens=False,
        )

        with torch.inference_mode():
            # Work around Transformers' generate() self-type mismatch.
            outputs = self.model.generate(  # type: ignore[misc]
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id,
            )

        # Decode only the new answer, excluding the original prompt.
        new_tokens = outputs[0, inputs["input_ids"].shape[1] :]
        return str(self.tokenizer.decode(new_tokens, skip_special_tokens=True)).strip()
