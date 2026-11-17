import requests
from bs4 import BeautifulSoup

url = "https://example.com"
response = requests.get(url, timeout=10)
response.raise_for_status()
print(response.status_code)

soup = BeautifulSoup(response.text, "html.parser")
if soup.title is None:
    print("No page title found")
else:
    print("Title:", soup.title.get_text(strip=True))

page_text = soup.get_text(" ", strip=True)
print("Text preview:", page_text[:500])