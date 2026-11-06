// The words a run sends with its SPARQL (`question` on /execute and /profile).
//
// Under a control plane the service records each question in the project's
// history, with its words when the SPARQL was generated from plain English and
// is run unedited (arango_sparql/service/recording.py; the service checks the
// words against what its /nl-translate generated). An edited query is the
// user's own, so it runs without the words.

/** The last plain-English question and the SPARQL generated from it. */
export interface AskedQuestion {
  question: string;
  sparql: string;
}

/** The question's words while `sparql` is still exactly what was generated for
 * it, else undefined (nothing asked, or the query was edited). */
export function questionFor(asked: AskedQuestion | null, sparql: string): string | undefined {
  return asked !== null && asked.sparql === sparql ? asked.question : undefined;
}
