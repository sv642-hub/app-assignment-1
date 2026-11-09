# User Brief: Smart AI Gateway

**Who is the user?** You are an AI engineer running a support assistant on a small team. You want each request sent to the cheapest capable model, and problems caught early. You play two roles: a bank customer in Chat, then the engineer in Show details and Health.

**Where should you go?** Application: http://149.165.151.225:8080. README: https://github.com/sv642-hub/app-assignment-1#readme

**How should you get started?** (1) In Chat, send three or four banking questions, such as "My card has not arrived." Include one vague message and one that is not about banking. (2) For each reply, click Show details and read the trace: the route (standard or complex), the intent, the reply source and the format check. (3) Open Health and find which model has the most samples (its recent calls) and whether either one was disabled. (4) Optional: open http://149.165.151.225:8080/docs and send one request through the API.

**What can it do, and what can't it do?** It recognizes ten banking intents, replies from a fictional bank's policy cards, shows each request's route, and disables a model that keeps failing. Everything is simulated: no real AI models run, and intents are matched by keywords. Each message stands alone, so when asked for details, resend the full problem. Past replies and the log of when models were disabled are kept when the app restarts; the failure counts and call totals start again from zero. Deployment, router-metric and fault-injection screens are not built yet.

**What feedback are we seeking?** Was the app's purpose clear within a minute? Which trace fields helped, and which confused you? What would you need on the Health screen to trust the routing? If answers got worse, what would you check first? Please file each finding as one GitHub issue on our repository (README link above): feature tested, what you expected, what actually happened.
