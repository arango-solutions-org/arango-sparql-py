import { describe, expect, it } from "vitest";

import { questionFor } from "./askedQuestion";

const asked = { question: "Who borrowed a book?", sparql: "SELECT ?n WHERE { ?m :fullName ?n }" };

describe("questionFor", () => {
  it("sends the words with the SPARQL generated for them, unedited", () => {
    expect(questionFor(asked, asked.sparql)).toBe("Who borrowed a book?");
  });

  it("sends no words once the query is edited, even by whitespace", () => {
    expect(questionFor(asked, `${asked.sparql} LIMIT 1`)).toBeUndefined();
    expect(questionFor(asked, `${asked.sparql}\n`)).toBeUndefined();
  });

  it("sends no words for a query written without asking", () => {
    expect(questionFor(null, asked.sparql)).toBeUndefined();
  });
});
