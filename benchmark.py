import time
import random
from digest import _keyword_token_forms

# Generate a list of tokens with some repetition
words = ["machine", "learning", "artificial", "intelligence", "neural", "networks", "deep", "learning", "computer", "vision", "natural", "language", "processing", "optimization", "stochastic", "gradient", "descent", "transformer", "attention", "mechanism", "generative", "adversarial", "networks", "diffusion", "models", "large", "language", "models", "reinforcement", "learning", "quantum", "computing", "cryptography", "blockchain", "distributed", "systems", "cloud", "computing", "edge", "computing", "internet", "things", "cybersecurity", "robotics", "autonomous", "vehicles", "bioinformatics", "computational", "biology"]

# Duplicate to simulate common words appearing many times
tokens = [random.choice(words) for _ in range(100000)]

start = time.time()
for token in tokens:
    _keyword_token_forms(token)
end = time.time()

print(f"Time taken for 100,000 calls: {end - start:.4f} seconds")
