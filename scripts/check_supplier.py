"""Read-only smoke test: balance, PUBG category lookup, offers. Spends nothing."""
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.fazercards import FazerCards  # noqa: E402


def show(title: str, data) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))


async def main() -> None:
    load_dotenv()
    client = FazerCards(
        os.environ["FAZERCARDS_API_KEY"],
        os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2"),
    )
    try:
        show("balance", await client.balance())

        cats = await client.all_categories()
        print(f"\nTotal categories: {len(cats)}")
        pubg = [c for c in cats if "pubg" in (c["category_id"] + c["name"]).lower()]
        show("PUBG categories", pubg)

        category_id = sys.argv[1] if len(sys.argv) > 1 else (pubg[0]["category_id"] if pubg else None)
        if category_id:
            show(f"offers for {category_id}", await client.offers(category_id))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
