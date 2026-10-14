# PB4HOST Prospector crawler policy

Publish this page at the URL named in `CRAWLER_USER_AGENT` / `CRAWLER_POLICY_URL`
(default `https://www.pb4host.com/prospector`) so site owners who see the user agent in their logs know who
we are and how to opt out.

---

**User agent:** `PB4HOST-Prospector/0.1 (+https://www.pb4host.com/prospector; prospector@pb4host.com)`

**Who we are.** PB4HOST is a WordPress support and hosting business based in Mangaluru, India. Our crawler
visits business websites to understand publicly visible technical facts — which CMS a site runs, whether it is
reachable, how fast it loads — so that we can offer relevant help to businesses that may need it.

**What the crawler does**

- Reads `robots.txt` first and obeys it, including `Disallow` rules for `*` and for our user agent.
- Fetches at most six public pages per site (home page and the contact/about pages linked from it), never more
  than one request per second per host, and never more than 2 MB per page.
- Only fetches ordinary pages. It does not log in, submit forms, probe admin paths, scan for vulnerabilities or
  download files.
- Re-visits a site only to confirm a visible error state (about an hour and a day later) or on a monthly refresh.

**What we store.** Technical facts about the website and the business contact details you publish on it
(business phone number, generic mailboxes such as `info@`, address, GST/CIN numbers, social links), each with
the page it came from and the date. Raw copies of fetched pages are deleted after 30 days. We do not store
information about individual people from your website.

**How to opt out or ask questions.** E-mail `prospector@pb4host.com` with your domain. We will add it to our
do-not-contact list within two working days (stored as a one-way hash so it can never be re-added), delete
what we hold if you ask, and confirm by reply. Adding `User-agent: PB4HOST-Prospector` / `Disallow: /` to your
`robots.txt` also stops all fetching immediately.

**Your rights.** Under India's Digital Personal Data Protection Act and, where applicable, the GDPR, you may ask
what we hold, ask for correction or deletion, and object to processing. The same e-mail address handles all
requests; we respond within seven days.
