import anthropic
from typing import Optional

AVAILABLE_MODELS = [
    ('claude-3-5-haiku-latest', 'Claude 3.5 Haiku (Fast)'),
    ('claude-sonnet-4-20250514', 'Claude Sonnet 4 (Balanced)'),
    ('claude-opus-4-5-20250514', 'Claude Opus 4.5 (Most Capable)')
]


class AnthropicService:
    """Service class for Anthropic API interactions"""

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def test_connection(self) -> tuple[bool, str]:
        """Test if the API key is valid by making a minimal request"""
        try:
            response = self.client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}]
            )
            return True, "Connection successful"
        except anthropic.AuthenticationError:
            return False, "Invalid API key"
        except anthropic.RateLimitError:
            return False, "Rate limit exceeded. Please try again later."
        except anthropic.APIConnectionError:
            return False, "Failed to connect to Anthropic API"
        except Exception as e:
            return False, f"Error: {str(e)}"

    def chat(self, messages: list, model: str, max_tokens: int = 4096) -> str:
        """Send a chat message and return the response"""
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages
        )
        return response.content[0].text
