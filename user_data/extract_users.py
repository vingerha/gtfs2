import requests

REPO = "vingerha/gtfs2"
TOKEN = "TOKEN FROM USER ACCOUNT"

headers = {"Authorization": f"token {TOKEN}"} if TOKEN else {}
 
def get_all(url, label):
    """Fetch all pages of a GitHub API list endpoint, following pagination.
    Returns a set of usernames. Stops and prints diagnostics on error."""
    users = set()
    page_count = 0
 
    while url:
        r = requests.get(url, headers=headers)
        page_count += 1
 
        if r.status_code != 200:
            print(f"\n[ERROR] {label}: request failed")
            print(f"  URL: {url}")
            print(f"  Status code: {r.status_code}")
            print(f"  Response: {r.json()}")
            if r.status_code == 401:
                print("  -> Likely cause: token missing or invalid.")
            elif r.status_code == 403:
                print("  -> Likely cause: rate limit exceeded. Check headers below.")
                print(f"  Rate limit remaining: {r.headers.get('X-RateLimit-Remaining')}")
                print(f"  Rate limit resets at (unix time): {r.headers.get('X-RateLimit-Reset')}")
            sys.exit(1)
 
        data = r.json()
 
        if not isinstance(data, list):
            print(f"\n[ERROR] {label}: expected a list, got {type(data)}")
            print(f"  Response: {data}")
            sys.exit(1)
 
        for item in data:
            login = item.get("login") or item.get("user", {}).get("login")
            if login:
                users.add(login)
 
        url = r.links.get("next", {}).get("url")
 
    print(f"[OK] {label}: fetched {len(users)} unique users across {page_count} page(s)")
    return users
 
 
def main():
    print(f"Fetching data for {REPO} ...")
    print(f"Authenticated: {'yes' if TOKEN else 'no (60 req/hour limit applies)'}\n")
 
    stargazers = get_all(f"https://api.github.com/repos/{REPO}/stargazers?per_page=100", "Stargazers")
    forks = get_all(f"https://api.github.com/repos/{REPO}/forks?per_page=100", "Forks")
    issues = get_all(f"https://api.github.com/repos/{REPO}/issues?state=all&per_page=100", "Issues")
 
    all_users = (stargazers | forks | issues)
    all_users.discard("vingerha")  # drop the repo owner
 
    print(f"\nTotal unique users (deduplicated, excluding owner): {len(all_users)}")
 
    with open("gtfs2_users.txt", "w") as f:
        for u in sorted(all_users):
            f.write(u + "\n")
 
    print("Saved to gtfs2_users.txt")
 
 
if __name__ == "__main__":
    main()
 