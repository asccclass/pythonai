---
name: laya
description: Write and improve programs that call Laya, the open-source Jev-compatible System-1 decision model. Use when designing Laya questions (choice, score, noul), structuring state, composing answers in code, setting confidence thresholds, choosing the Python or @receptron/laya TypeScript runtime, or diagnosing a Laya question that answers wrong or with low confidence.
---

# Writing and improving Laya programs

Laya is a judgment model. It reads one `state`, answers every typed question in the request in one forward pass, and returns probabilities over the answer space you defined. It does not reason in steps and it does not generate text. Code owns the control flow, the arithmetic, and the policy. Laya owns the snap judgments.

A good Laya question is one a knowledgeable person answers in a second, given the right context. "Does this message convey urgency?" fits. "Analyze this message and decide what to do" does not fit. Break that task into small questions and compose the answers in code.

This guidance covers two practical runtimes:

- Python/reference style, used in this repo's `laya_guard.py`: `agent.predict(state, QUESTIONS)` with question keys such as `type: "choice"`, `type: "score"`, and `type: "noul"`.
- Node/TypeScript style from `@receptron/laya`: `const laya = await Laya.load(); await laya.systemOne(state, questions)`.

The request shape is intentionally Jev-compatible across runtimes, but the method names and object casing differ. Keep examples runtime-specific.

## Runtime quick reference

### Python/reference style

Use this style when working inside this Python bot or any code already using `laya.load()`.

```python
import laya

agent = laya.load("models/laya-multilingual")
result = agent.predict(
    {"message": "Please delete this file"},
    {
        "intent": {
            "type": "choice",
            "instructions": "What does the user want the local assistant to do?",
            "criteria": {
                "chat": "general conversation",
                "read_file": "read a local file",
                "delete_file": "delete a local file",
            },
        },
        "needs_confirmation": {
            "type": "noul",
            "instructions": "Should this request require explicit user confirmation before execution?",
        },
    },
)

intent = result["answers"]["intent"]["choice"]
needs_confirmation = result["answers"]["needs_confirmation"]["noul"]
```

### TypeScript / `@receptron/laya` style

Use this style when building a Node.js or TypeScript app. Install with:

```sh
npm install @receptron/laya
```

Node.js 20 or newer is required. The ONNX bundle is downloaded on first use and cached. Budget roughly 2 GB of RAM for the loaded model plus extra memory for large batches.

```ts
import { Laya } from "@receptron/laya";

const laya = await Laya.load();

const result = await laya.systemOne(
  {
    subject: "Refund not received",
    body: "I cancelled two weeks ago and still have no refund.",
  },
  {
    department: {
      type: "choice",
      instructions: "Which team should handle this ticket?",
      criteria: {
        billing: "payments, refunds, invoices",
        support: "product help and bugs",
        sales: "new purchases and upgrades",
      },
    },
    urgency: {
      type: "score",
      instructions: "How urgent is this ticket?",
      criteria: ["not urgent", "somewhat urgent", "urgent", "critical"],
    },
    churn_risk: {
      type: "noul",
      instructions: "Is the customer likely to cancel or dispute?",
    },
  },
);

console.log(result.answers.department.choice);
console.log(result.answers.department.probabilities);
console.log(result.answers.urgency.score);
console.log(result.answers.churn_risk.noul);

await laya.close();
```

`Laya.load()` options to remember:

```ts
await Laya.load({
  modelDir: "./onnx",
  repo: "receptron/laya-onnx",
  subfolder: "multilingual",
  revision: "main",
  cacheDir: "/var/cache/laya",
  token: process.env.HF_TOKEN,
  executionProviders: ["cpu"],
  sessionOptions: { intraOpNumThreads: 4 },
});
```

Prefer `modelDir` for reproducible local deployments. Pin `revision` when using a remote Hugging Face bundle in production. Call `close()` when a long-lived process is done with the model.

## Workflow

1. List the decisions your code must make. Write each as a branch, a threshold, or a ranking.
2. Write one question per judgment. Split any question that weighs two properties.
3. Pick the primitive whose answer your code acts on directly.
4. Build the smallest state that answers every question. Compute in code whatever code can compute.
5. Put every question that shares the state into one request, including questions that matter for only some inputs.
6. Combine the answers in code: branches, weights, and confidence gates.
7. Test against labeled examples. Read `probabilities` on the misses, then revise one or two questions at a time.

## Choose the primitive

| Primitive | Use it when | Returns | Code acts on it with |
| --- | --- | --- | --- |
| Choice / `choice` | The answer is one of a known set with no order | `choice`, `probabilities`, `confidence` | a branch per option |
| Score / `score` | The answer is a position on a spectrum you can describe in steps | `score`, `probabilities`, `confidence`, `legend` | a threshold, a rank, or a weight |
| Noul / `noul` | The answer is a clean yes or no and the probability is the signal | `noul`, from 0 to 1 | an `if` on a threshold |

- Add an `other` or `none of the above` option to a Choice whose list may not cover every input.
- A Noul of 0.5 means laya is unsure. It does not mean "medium". Use a Score to measure a degree.
- A Noul needs a crisp condition. "Is this candidate strong in Python?" is vague. "Does the resume state that the candidate used Python at work?" is crisp.
- Use a Choice or several Nouls when the answer has no in-between.

## Write the instructions

- State the exact condition. laya answers the words you wrote. It reads scoping words, negations, and implied conditions at face value.
- Ask one property per question. Hidden second judgments lower accuracy and confidence.
- Name the part of the state the question judges, with a backticked path: `` `ticket.messages[0].text` ``.
- Write directly. Avoid double negatives, a property of a property, and any question that needs several hops.
- Keep numerals that stand for levels out of the instructions. "Rate from 0 to 2" gives laya nothing to match.
- Write the full question in `instructions`. The question ID never reaches the model.
- Keep decision policy out of the question. "A shared address cannot override a name conflict" belongs in code.
- When you explain what you really meant after a wrong answer, that explanation is the missing half of the instruction. Add it.

Instructions accept a string or an object in the TypeScript runtime. The Python/reference runtime also accepts structured instruction payloads in practice. Use an object when the question has labeled parts or needs supporting data. Pass a schema, taxonomy, or row as JSON. Do not serialize it into a string template.

```json
"instructions": {
  "question": "Does the `message` ask the recipient to disclose a sensitive credential?",
  "inspect": "message",
  "focus": "Look for a request to send the credential itself, not a request to change or reset it."
}
```

Useful keys from the docs: `question`, `focus`, `inspect`, `note`, `compare` (a list of state paths), and `field` (a `name`, `type`, `unit`, `description` record that several questions share).

## Write the criteria

Treat criteria as an extension of the instruction. The two must ask for the same thing, in the same direction. A Noul whose `true` side describes "no" performs worse.

**Choice.** Map each option to a description. Make the descriptions contrastive when options sit close together:

```json
"billing": {
  "what": "Charges, invoices, refunds, or subscriptions",
  "not_for": "Order tracking or account access",
  "examples": ["I was charged twice", "Where is my refund?"]
}
```

**Score.** List the levels from low to high. Use two to ten levels, and only as many as you can describe distinctly.

- Describe situations. "Broken feature, but a workaround exists" works. "Moderately severe" does not.
- Make each level stand alone. laya judges each level separately and sees neither its number nor its neighbors. "Worse than the previous level" means nothing to it.
- Keep each Score to one dimension. "Punctual and smart and experienced" measures three things.
- Give a rare extreme its own level when your code must treat it differently.
- A level may be an object: `{"summary": "One change, clearly stated", "signals": ["A single fix or feature", "..."]}`.

**Noul.** Criteria are optional. Add `true` and `false` sides, each with `what` and `examples`, when the boundary is subtle. Put the neighboring case in the description of the side it belongs to.

**Examples.** Write short concrete instances, such as "I was charged twice". Do not write a description of an instance, such as "a message about a billing problem".

## Build the state

- Send only the fields the questions need. Unrelated detail lowers accuracy, and a large state hides which input caused a wrong answer.
- Retrieve and filter in code first. When code cannot filter, ask a relevance Noul per passage and keep the passages that pass.
- Keep the state structured so questions can point into it by path.
- Convert numeric encodings to words before sending them. Send a color name in place of a hex value. Send a computed number or a named bucket in place of raw figures.
- Compute date order, duration, windows, counts, and sums in code. Send the result.
- Runtime limits matter. In the current `@receptron/laya` package, each question's option text must fit in `head_max_len` (192) tokens, and the state is truncated to `max_len` (512 tokens for the English checkpoint) after the question header.
- Keep Choice option counts small. The package README recommends fewer than about 20 options for a Choice question.
- A JSON state in the TypeScript runtime is serialized to match Python's `json.dumps(ensure_ascii=False)` behavior as closely as JavaScript allows.
- Treat text in the state as able to steer the answer. laya does not treat state as hostile. State in the criteria what counts, and test injected and self-describing content before deployment.

## Compose the answers in code

**Speculative fan-out.** Ask every question your code might need in one request, including questions that matter only on some branches. Questions are batched into one run, so extra questions add little latency compared with serial calls. Code ignores the answers it does not need.

**Second requests.** Make a second request only when code cannot build it without the first answer, such as when the answer decides what data to fetch. Questions in one request never see each other's answers.

**Confidence-gated routing.** The answer says what. Confidence says whether to act. Set a floor below which no action runs, then set a threshold per action that rises with the cost of a wrong call. The docs use floors of 0.5 to 0.6 and 0.85 to 0.9 for high-stakes actions. Start conservative and tune on your data. The three standard paths are: act, confirm or flag, and hand off.

**Composite scoring.** Split a complex judgment into one Score per dimension. Normalize each score by `len(criteria) - 1`, then combine with weights in code. Change a weight when priorities shift. Do not rewrite a question to change policy.

**Intent routing.** Classify with a Choice, and add a complexity Score beside it. Route each intent to deterministic code, a specialist LLM, or a person. Send low-confidence classifications to a person.

**Taxonomy walk.** Ask one Choice per tree level and walk the tree in code. Give each option its subtree as the criteria value, so laya sees what lives under a branch. Trim large subtrees to direct children and a sample of leaves. Follow several branches when the probabilities are close.

**Counting.** laya does not count. Ask one Noul per item in a single request, then sum the answers that pass your threshold.

**Dates.** Extract each date part with a Choice over enumerated options, including a "not stated" option. Assemble and compare the date in code.

**Extraction.** Generate candidates with a regex or a generative model. Ask laya to pick among them with a Choice, or to verify one with a Noul.

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

SEVERITY = [
    "Cosmetic; no impact to functionality",
    "Broken or degraded feature; a workaround exists",
    "Blocking issue; no workaround exists",
]

with TypeSafeClient() as client:
    response = client.system_one(
        state={"ticket": ticket_text},
        questions={
            "category": Choice(
                instructions="Which category fits the main request in `ticket`?",
                criteria={
                    "bug_report": "Something is broken or producing errors",
                    "billing": "Charges, invoices, refunds, or subscriptions",
                    "other": "Anything else",
                },
            ),
            # Speculative: only used when the ticket is a bug report.
            "severity": Score(instructions="How severe is the issue reported in `ticket`?", criteria=SEVERITY),
            "refund_requested": Noul(instructions="Does `ticket` explicitly ask for a refund or credit?"),
        },
    )

category = response.answers["category"]
severity = response.answers["severity"]
if category.confidence < 0.6:
    route_to_human(ticket_id)
elif category.choice == "bug_report":
    normalized = severity.score / (len(SEVERITY) - 1)
    if normalized > 0.75 and severity.confidence > 0.5:
        escalate(ticket_id)
    else:
        add_to_backlog(ticket_id)
elif category.choice == "billing" and response.answers["refund_requested"].noul > 0.7:
    route_to_billing(ticket_id, refund_likely=True)
```

## Read the answers

- `score` is the probability-weighted mean of the level numbers. A score of 1.0 can mean certainty on level 1 or an even split between levels 0 and 2. Read `probabilities` with it.
- Threshold a score, rank by it, or round it to the nearest level. Do not interpolate a quantity from it. laya's levels are weakly calibrated as numbers.
- `confidence` measures how peaked the distribution is. It describes the model's answer and does not guarantee a correct one. The full `probabilities` are there when you need a different statistic.
- A Noul has no `confidence`. Its distance from 0.5 plays that role.
- Every answer stays inside the options you supplied, so code never parses prose.
- TypeScript answer types follow question types: `ChoiceAnswer`, `ScoreAnswer`, or `NoulAnswer`.
- TypeScript results include `usage.input_tokens` and `usage.output_tokens`; output tokens are zero because Laya does not generate text.
- The TypeScript implementation also exposes `rl_agent.act_probability` on answers. Treat it as a model diagnostic, not as your primary business rule.

## TypeScript implementation notes

Use these when reviewing or debugging code that imports `@receptron/laya`.

- The package exports `Laya`, bundle helpers, and TypeScript request/response types.
- `Laya.load()` resolves either a local `modelDir` or downloads a Hugging Face ONNX bundle into cache.
- The ONNX bundle consists of `laya.onnx`, `laya.onnx.data`, `laya_config.json`, `tokenizer/tokenizer.json`, and `tokenizer/tokenizer_config.json`.
- The default published bundle repo is `receptron/laya-onnx`; cache defaults to `LAYA_CACHE` or `~/.cache/receptron-laya`.
- `systemOne()` throws if no questions are supplied.
- `systemOne()` builds one sequence per question, right-pads the batch, runs ONNX once, applies calibrated softmax, and rounds answer probabilities to four decimals.
- Noul internally uses `[false, true]`; `noul` is the probability of the true side.
- Score internally renders criteria as ordered levels starting at zero and returns the expected level.
- Choice criteria may be an object mapping option to description, or a list of option names in the TypeScript type.
- Object instructions are JSON-stringified before being rendered into the sequence.

## Improve a program

Find the question that fails before changing anything. Collect labeled examples, run them, and compare each question's answers and probabilities against the labels.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Wrong answers with high confidence | laya read the instruction literally | State the exact condition. Put the boundary case in the criteria. |
| Low confidence on a Choice | Options overlap, or no option fits | Add `what`, `not_for`, and `examples`. Add an `other` option. |
| Low confidence on a Score | Levels overlap, the question measures two things, or the state says too little | Rewrite levels as distinct situations. Split the question. Add the missing field to the state. |
| Scores cluster in the middle | Levels are degrees or numbers | Describe a concrete situation per level. Remove numerals. |
| Top-of-scale cases look alike | The extreme case has no level | Add a level for the extreme. |
| A Noul hovers near 0.5 | The condition is vague | Define the condition. Add `true` and `false` criteria with examples. |
| Accuracy falls as inputs grow | The state carries irrelevant detail | Filter in code. Send only the needed fields. |
| Errors on counts, sums, dates, or numeric nearness | laya is doing arithmetic | Move the arithmetic to code. Ask laya only for extraction or per-item judgments. |
| Errors on nested or negated questions | Too much indirection | Ask the direct question. Name the state path. Split into two literal questions and combine in code. |
| The answer follows text inside the state | The content steers the model | Tighten the criteria. Test adversarial cases. Gate the action on confidence. |
| Rewording one question trades one error for another | One question weighs several properties | Split it into atomic questions and combine them in code. |
| The final decision is wrong while each answer is right | The policy is wrong | Change the weights or thresholds in code. Leave the questions alone. |
| The program is slow or costly | Questions are spread over sequential calls | Merge them into one request. Keep a second request only when it truly depends on the first answer. |

Rules for revising:

- Change one or two questions per revision. laya's probabilities shift in ways that are hard to predict, so leave questions that discriminate well untouched.
- Judge a revision on labeled data. Higher confidence alone does not show a better question, and two wordings of one scale can behave differently on your data.
- Keep the answer space stable once code depends on it. Adding or removing a level or an option changes what every earlier answer meant.
- Write general rules in instructions and criteria. Put specific names and values only in `examples`.

## Checklist

- [ ] Each question asks one property and a person could answer it in a second.
- [ ] The primitive matches how code uses the answer.
- [ ] Instructions state the exact condition and name state paths in backticks.
- [ ] Criteria agree with the instructions and point the same direction.
- [ ] Score levels describe situations, stand alone, and carry no numerals.
- [ ] Choices that may not cover every input have an `other` option.
- [ ] Code does all counting, arithmetic, and date comparison.
- [ ] The state holds only what the questions need and fits the token limits.
- [ ] All questions on the same state travel in one request.
- [ ] Every action has a confidence threshold matched to its risk, and low confidence has a fallback.
- [ ] Weights and thresholds live in code.
- [ ] Labeled examples back every revision.

## Installation and deployment checklist

- [ ] For TypeScript, require Node.js 20 or newer.
- [ ] Decide between remote bundle download and pinned local `modelDir`.
- [ ] Budget disk/network for a roughly 1.7 GB fp32 ONNX bundle.
- [ ] Budget about 2 GB RAM for the loaded model plus extra memory for large batches.
- [ ] Set `LAYA_CACHE` if the default user cache is not suitable.
- [ ] Set `HF_TOKEN` or `token` only when a private Hugging Face repo is used.
- [ ] Pin `revision` for reproducibility.
- [ ] Call `await laya.close()` in scripts, workers, and tests when finished.
- [ ] Keep model tests optional in CI unless the ONNX bundle is present.

## Sources

- https://github.com/receptron/laya
- https://raw.githubusercontent.com/receptron/laya/main/README.md
- https://raw.githubusercontent.com/receptron/laya/main/src/types.ts
- https://raw.githubusercontent.com/receptron/laya/main/src/laya.ts
- https://raw.githubusercontent.com/receptron/laya/main/src/sequence.ts
- https://raw.githubusercontent.com/receptron/laya/main/src/download.ts
