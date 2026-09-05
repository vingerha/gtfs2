BATCH_SIZE = 40  # keep under ~20 to stay clear of GitHub's mention/notification limit per comment
INPUT_FILE = "gtfs2_users.txt"
OUTPUT_FILE = "mention_batches.txt"

INTRO_TEXT = "Tagging folks who've starred, forked, or commented on this repo — your input would help a lot 🙏"


def chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def main():
    with open(INPUT_FILE) as f:
        users = [line.strip() for line in f if line.strip()]

    print(f"Loaded {len(users)} users from {INPUT_FILE}")

    batches = list(chunk(users, BATCH_SIZE))
    print(f"Split into {len(batches)} batch(es) of up to {BATCH_SIZE} users each\n")

    with open(OUTPUT_FILE, "w") as out:
        for i, batch in enumerate(batches, start=1):
            mentions = " ".join(f"@{u}" for u in batch)
            block = f"{INTRO_TEXT} {mentions}"

            out.write(f"--- Batch {i} of {len(batches)} ---\n")
            out.write(block + "\n\n")

            print(f"--- Batch {i} of {len(batches)} ---")
            print(block)
            print()

    print(f"All batches also saved to {OUTPUT_FILE} — paste each block as a separate comment, spaced a few minutes apart.")


if __name__ == "__main__":
    main()
