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
- Re-visits a site only to confirm a visible error state (about an hour and a day later), on a monthly refresh, or
  weekly (home page only) for businesses we are already talking to or hosting.

**Other public sources we consult.** Besides your own website we look at aggregate, non-personal public data:
Google's Chrome UX Report and the HTTP Archive (how fast websites load for real visitors, per website, no visitor
data), the GitHub profile pages of software companies (company name, website, public repository counts — never
member lists or e-mail addresses, and never used for unsolicited e-mail) and, for UK companies, the public
Companies House register (company name, number, registered office). Officer names shown by Companies House are
looked up live when one of us views a company and are not stored.

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
requests; we respond within seven days and in any case within thirty.

---

# Privacy notice for enquiries and contacts (publish at `PRIVACY_NOTICE_URL`)

**When you fill in a form on pb4host.com** (speed evaluation, agency partnership) we store what you typed, the
consent statement you ticked with its version and time, and a one-way hash of your IP address, so we can answer
you and prove you asked us to. We also look up public technical facts about the website you named (see above) so
our reply is useful. Your details go into our customer system (self-hosted EspoCRM in our own infrastructure) with a
copy of this notice. We keep enquiry records for as long as we are in contact and delete contact details six months
after the last exchange if nothing came of it.

**If you use "Sign in with LinkedIn" on our form**, LinkedIn sends us only your name, e-mail address and profile
picture URL after you approve it; we use the one-time token to read those fields and discard it. We never read your
connections, posts or activity, and we never log in to LinkedIn on your behalf.

**If you are connected to our founder on LinkedIn**, his own connections export (name, headline, company, the date
you connected and your e-mail only if you chose to share it with connections) may be used to recognise that we
already know someone at a company we are looking at, on the basis of legitimate interest (assessment reference
`LIA-2026-01`). This is only ever used for a personal one-to-one message from him, never for mailing lists or
exports, and you can object at any time.

**How to exercise your rights.** E-mail `privacy@pb4host.com` (or reply to any message from us) to access, correct
or delete your data, withdraw consent or object. We log every request, act within seven days where we can and at
most within thirty, and keep a one-way hash of your identifier afterwards so you are never contacted again.
