#!/usr/bin/env python3
"""Write markdown test files of 100 KB, 1 MB and 10 MB to bench/fixtures (not committed).

The content mixes what markdown files usually contain: headings, paragraphs with emphasis, links and
inline code, lists, task lists, quotes, code blocks and tables. The same files come out every time.
"""

import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench/fixtures"
SIZES = {"100k.md": 100_000, "1m.md": 1_000_000, "10m.md": 10_000_000}

WORDS = (
    "markdown editor file text line write read fast small memory window sidebar recent open save "
    "the a of and to in is it that for on with as this was are be at by from or an not but can"
).split()


def sentence(rng):
    words = [rng.choice(WORDS) for _ in range(rng.randint(6, 18))]
    i = rng.randrange(len(words))
    words[i] = rng.choice(["**{}**", "*{}*", "`{}`", "[{}](https://example.com/{})"]).format(words[i], words[i])
    return " ".join(words).capitalize() + "."


def block(rng, n):
    kind = rng.random()
    if kind < 0.08:
        return f"{'#' * rng.randint(1, 3)} Section {n}"
    if kind < 0.55:
        return " ".join(sentence(rng) for _ in range(rng.randint(2, 6)))
    if kind < 0.70:
        return "\n".join(f"- {sentence(rng)}" for _ in range(rng.randint(2, 6)))
    if kind < 0.78:
        return "\n".join(f"- [{rng.choice(' x')}] {sentence(rng)}" for _ in range(rng.randint(2, 5)))
    if kind < 0.85:
        return "> " + sentence(rng)
    if kind < 0.95:
        lines = [f"    let value_{i} = compute({i}, \"{rng.choice(WORDS)}\");" for i in range(rng.randint(3, 10))]
        return "```rust\nfn example() {\n" + "\n".join(lines) + "\n}\n```"
    rows = "\n".join(f"| {rng.choice(WORDS)} | {rng.randint(0, 999)} | {rng.choice(WORDS)} |" for _ in range(4))
    return "| Name | Count | Note |\n| --- | ---: | --- |\n" + rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        rng = random.Random(name)
        parts, length, n = ["# markL test file\n"], 0, 0
        while length < size:
            n += 1
            part = block(rng, n)
            parts.append(part)
            length += len(part.encode()) + 2
        (OUT / name).write_text("\n\n".join(parts) + "\n")
        print(f"{OUT / name}: {(OUT / name).stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
