import os
import time
import requests


OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://127.0.0.1:11434/api/generate"
)

OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL",
    "gemma3:latest"
)

OLLAMA_TIMEOUT = int(
    os.getenv("OLLAMA_TIMEOUT", "600")
)


def ask_ollama(prompt, model=OLLAMA_MODEL):
    """
    Send a prompt to the local Ollama server
    and return the generated response.

    This function is intentionally generic.
    It does not contain UART or any other
    design-specific information.
    """

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }

    print("\n========================================")
    print("Connecting to Ollama...")
    print(f"URL   : {OLLAMA_URL}")
    print(f"Model : {model}")
    print("========================================")

    start_time = time.time()

    try:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )

        elapsed = time.time() - start_time

        response.raise_for_status()

        data = response.json()

        print(
            f"Ollama response received "
            f"in {elapsed:.1f} seconds"
        )

        if "response" not in data:
            raise RuntimeError(
                "Ollama response does not contain "
                "'response' field."
            )

        return data["response"]

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "Cannot connect to Ollama.\n"
            "Make sure Ollama is running on "
            "127.0.0.1:11434."
        )

    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Ollama request timed out after "
            f"{OLLAMA_TIMEOUT} seconds."
        )

    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(
            f"Ollama HTTP error: {exc}"
        )

    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Ollama request failed: {exc}"
        )


if __name__ == "__main__":

    # Generic connection test.
    # There is NO UART-specific prompt here.

    test_prompt = """
Reply with exactly:

OLLAMA CONNECTION SUCCESSFUL
"""

    try:
        answer = ask_ollama(test_prompt)

        print(
            "\n========== OLLAMA RESPONSE ==========\n"
        )

        print(answer)

        print(
            "\n======================================\n"
        )

    except Exception as exc:
        print(f"\nERROR: {exc}")