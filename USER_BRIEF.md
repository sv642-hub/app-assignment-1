# User Brief: Smart AI Gateway

**Who is the user?** You run one AI feature in production, such as a customer-support assistant, on a small team. You want each request sent to the cheapest model that can handle it, and you want to know when answers quietly get worse before your customers notice.

**Where should you go?** Application: http://149.165.151.225:8080. README: https://github.com/sv642-hub/app-assignment-1#readme

**How should you get started?** (1) In Chat, send three or four banking questions, such as "My card has not arrived." Include one vague message and one that is not about banking. (2) For each reply, click Show details and read the trace: the route (standard or complex), the intent, the reply source and the format check. (3) Open Health and find which model has the most samples (its recent replies) and whether either one was disabled. (4) Optional: open http://149.165.151.225:8080/docs and send one request through the API.

**What can it do, and what can't it do?** It recognizes ten banking intents, replies from a fictional bank's policy cards, shows how each request was routed, and disables a model whose replies keep failing the format check. Everything is simulated: no real AI models run, intents are matched by keywords, and all data resets when the app restarts. Screens for deployments, router metrics and fault injection are not built yet.

**What feedback are we seeking?** Was the app's purpose clear within a minute? Which trace fields helped, and which confused you? What would you need on the Health screen to trust the routing? If answers got worse, what would you check first? Please send your answers to [FEEDBACK LINK].
