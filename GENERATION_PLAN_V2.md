The Automated Dispatch Architecture
In this setup, your Python script is the "Dispatcher," reading one entry from topics.json randlomly ensure do not generate dublicate  at a time and passing it through a 4-step assembly line.

Step 1: The Researcher Agent (Gemini Key 1 - Flash)

The Action: Python reads a topic from your JSON (e.g., "Exam: NORCET, Topic: Pharmacology/Calcium Regulators"). It sends this context to Gemini Flash.

The Prompt: "I am generating questions for the [Insert Exam] on the topic [Insert Topic]. Provide a strict, bulleted summary of the expected question format, difficulty level, and top 5 core concepts tested in this specific exam for this topic." you can imrpove the prompt according to the project and the exama nd class so that it properly reasearch for the exam and the subejcts proeprly with no errors

Why Flash: Flash is lightning fast and has high rate limits. It acts as the prompt-engineer for the next step, ensuring the generated questions match the exact standard of the exam without wasting your precious Pro limits.

Step 2: The Generator Agent (Gemini Key 1 - Pro)

The Action: Python takes the research context from Step 1 and the required question count from your JSON. Crucial: If your JSON asks for a 50-question chapter test, Python MUST chunk this into loops of 20 questions to survive the free tier limits but if hard and very high level questiosna dn their solutions like jee mains and advanced then only 10 or less than 10 question per loop which is suitable for the free tier limits but enusre proper content generation but for class 20 or more questions are good to go but you cna properly imrpvoe according to the class and exam .

The Prompt: "Using this exam research: [Insert Flash Output], generate 5 high-difficulty questions for the [Insert Exam] on [Insert Topic]. Include tricky options and a detailed, step-by-step deductive solution for each. Output in raw text." you can imrpove the promp thsi is just a example

The Loop: Python waits 35 seconds after this call before moving to the next step to respect Key 1's limits.

Step 3: The Auditor Agent (Gemini Key 2 - Pro)

The Action: Pass the raw questions generated in Step 2 to your second Google account.

The Prompt: "Review these questions for the [Insert Exam]. Recalculate any math, verify medical accuracy, and ensure there are no hallucinated facts. Return the corrected questions, options, and solutions in raw text."

Why Key 2: Key 1 is already heavily burdened with generation. Offloading the review process to Key 2 effectively doubles your daily Pro capacity and acts as a strict quality control net. Wait 35 seconds after this call.

Step 4: The Formatter Agent (Groq - Llama 3 8B)

The Action: Pass the audited, flawless text to Groq.

The Prompt: "Extract the questions, options, correct answers, and explanations from this text. Output strictly matching this JSON schema: [Insert Your Schema]. Output absolutely nothing but valid JSON."

The Result: Groq processes this instantly using a fraction of a penny's worth of tokens. Python receives perfect JSON, appends it to your final output file, and either loops back to Step 2 for the next chunk of questions, or moves to the next entry in topics.json

and also want proper notes and resources and formulas with good qwality and long content generation about the topic 

so per run generate one full lenght test and notes per run and resources and all in proper formated images required then put links required then put and in proper way per run but be in the free qotaa limit 
and properly ensure that the workflow do not break means do not cancel so proper fallback and error handling must be there