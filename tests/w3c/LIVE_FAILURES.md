# W3C live failure classifications

> Generated file. Edit `LIVE_FAILURE_LABELS.json`, then run the regeneration command below.

## Baseline

- Commit: `e94776922f7f02bced97da6e706caef5d0d53811`
- Storage profile: `document_edge`
- Passing: `131/191`
- Xfailed: `60`

## Label summary

| Label | Count |
| ----- | ----: |
| needs inference | 18 |
| language tags lost | 8 |
| text stored the wrong way | 3 |
| genuine bug | 31 |

## Classified failures

| Test ID | Label | Diagnosis |
| ------- | ----- | --------- |
| `bind/bind07` | genuine bug | The translator evaluates UNION inside the outer FOR, so each inner BIND incorrectly sees outer ?o and binds ?z. |
| `bind/bind10` | genuine bug | The translator exposes the outer BIND value ?z inside the nested group FILTER even though ?z is not in scope there. |
| `bindings/values4` | genuine bug | The translator emits equality against the UNDEF VALUES cell instead of treating that cell as an unbound compatible slot. |
| `bindings/values5` | genuine bug | The translator emits equality constraints for both UNDEF VALUES cells, rejecting every otherwise compatible solution. |
| `bindings/values7` | genuine bug | The translator compares VALUES to a failed OPTIONAL result instead of allowing VALUES to bind the still-unbound ?o2. |
| `bindings/values8` | genuine bug | The translator emits equality against each UNDEF VALUES cell, so neither partially bound VALUES row can join. |
| `construct/constructwhere04` | genuine bug | The live harness does not load the query-level FROM <data.ttl> dataset, so the CONSTRUCT runs over an empty collection. |
| `entailment/lang` | language tags lost | The loader flattens the explicit language-tagged literal, so the query cannot match its @en language tag. |
| `entailment/paper-sparqldl-Q1` | needs inference | The expected ConferencePaper binding follows from the declared OWL restriction rather than an explicit matching triple. |
| `entailment/paper-sparqldl-Q1-rdfs` | needs inference | The expected binding requires the manifest's RDFS entailment regime to apply the subclass relation. |
| `entailment/paper-sparqldl-Q2` | needs inference | John is explicitly a GraduateAssistant, but membership in the queried Student and Employee intersection requires OWL and RDFS inference. |
| `entailment/plainLit` | text stored the wrong way | The loader flattens plain and xsd:string literals to the same primitive text, erasing the distinction tested by the query. |
| `entailment/rdf01` | needs inference | The expected rdf:type predicate binding requires RDF entailment to infer that the used predicate ex:b is an rdf:Property. |
| `entailment/rdfs01` | needs inference | The second expected predicate is absent explicitly and follows from ex:b1 rdfs:subPropertyOf ex:b2. |
| `entailment/rdfs02` | needs inference | The expected bindings require RDFS subPropertyOf and domain entailment beyond the explicit triples. |
| `entailment/rdfs05` | needs inference | The expected binding requires transitive RDFS subPropertyOf entailment. |
| `entailment/rdfs08` | needs inference | The expected types require RDFS subClassOf and rdfs:Resource entailment. |
| `entailment/rdfs10` | needs inference | The expected binding requires reflexive RDFS subClassOf entailment that is not an explicit triple. |
| `entailment/rdfs11` | needs inference | The ex:p row requires both subproperty propagation and reflexive rdfs:subPropertyOf entailment. |
| `entailment/rdfs12` | needs inference | The expected membership binding requires RDFS ContainerMembershipProperty and rdfs:member entailment. |
| `entailment/simple1` | needs inference | The anonymous intersection class is not explicit in the data, so the expected :A and :B members require OWL intersection inference. |
| `entailment/sparqldl-02` | needs inference | The expected binding depends on OWL DL reasoning rather than an explicit matching graph pattern. |
| `entailment/sparqldl-03` | needs inference | The expected :c class row requires reflexive RDFS subClassOf entailment while the property match is explicit. |
| `entailment/sparqldl-10` | needs inference | The expected binding depends on OWL DL reasoning rather than an explicit matching graph pattern. |
| `entailment/sparqldl-11` | needs inference | The expected binding depends on OWL DL reasoning rather than an explicit matching graph pattern. |
| `entailment/sparqldl-12` | needs inference | The expected binding depends on OWL DL reasoning rather than an explicit matching graph pattern. |
| `entailment/sparqldl-13` | needs inference | The expected binding depends on OWL DL reasoning rather than an explicit matching graph pattern. |
| `exists/exists03` | genuine bug | The live harness does not load the manifest's qt:graphData file, so the named graph tested by EXISTS is empty. |
| `functions/bnode01` | genuine bug | The translator hashes BNODE labels globally, but the expected blank-node identity is scoped per solution while repeated labels within one solution stay equal. |
| `functions/concat02` | genuine bug | The translator lets AQL CONCAT coerce numeric 7 to text instead of leaving CONCAT unbound on the SPARQL type error. |
| `functions/hours` | genuine bug | The translator uses AQL DATE_HOUR, which normalizes the -08:00 value to 23 instead of returning its lexical hour 15. |
| `functions/if01` | language tags lost | The loader flattens the @ja literal, so LANG always appears empty and IF returns false for the Japanese row. |
| `functions/now01` | genuine bug | The translator represents NOW() as an AQL string, so DATATYPE reports xsd:string instead of xsd:dateTime. |
| `functions/plus-1` | genuine bug | The loader drops the blank-node :p value, and the resolver and translator choose only the edge mapping for mixed-object :p, omitting six stored literals. |
| `functions/plus-2` | genuine bug | The loader drops the blank-node :p value, and the resolver and translator choose only the edge mapping for mixed-object :p, omitting six stored literals. |
| `functions/rand01` | genuine bug | The translator classifies every AQL number as xsd:integer, so DATATYPE(RAND()) cannot equal xsd:double. |
| `functions/replace01` | genuine bug | The translator lets AQL REGEX_REPLACE coerce numeric 7 to text instead of leaving REPLACE unbound on the SPARQL type error. |
| `functions/strafter01a` | genuine bug | The translator applies STRAFTER to numeric 7 and returns an empty string instead of leaving the expression unbound on a type error. |
| `functions/strafter02` | language tags lost | Flattened language tags make incompatible @cy and @en arguments look like ordinary strings, producing bindings that should be errors. |
| `functions/strbefore01a` | genuine bug | The translator applies STRBEFORE to numeric 7 and returns an empty string instead of leaving the expression unbound on a type error. |
| `functions/strbefore02` | language tags lost | Flattened language tags make incompatible @cy and @en arguments look like ordinary strings, producing bindings that should be errors. |
| `functions/strdt01` | language tags lost | The loader flattens the @en literal, so LANGMATCHES sees an empty language and rejects the only expected row. |
| `functions/strdt02` | language tags lost | The loader flattens the @en literal, so LANGMATCHES sees an empty language and rejects the only expected row. |
| `functions/strdt03` | text stored the wrong way | Flattened RDF term kinds let STRDT accept numeric, date, language-tagged, and already typed values that should raise expression errors. |
| `functions/strlang01` | language tags lost | The loader flattens the @en literal, so LANGMATCHES sees an empty language and rejects the only expected row. |
| `functions/strlang02` | language tags lost | The loader flattens the @en literal, so LANGMATCHES sees an empty language and rejects the only expected row. |
| `functions/strlang03` | text stored the wrong way | Flattened RDF term kinds let STRLANG accept numeric, date, language-tagged, and already typed values that should raise expression errors. |
| `project-expression/projexp05` | genuine bug | The resolver and translator choose only the edge mapping for the mixed literal and IRI predicate, omitting the stored numeric value and typing the IRI as xsd:string. |
| `property-path/pp07` | genuine bug | The live harness does not load the manifest's qt:graphData file, so the named graph containing the two-step path is empty. |
| `property-path/pp10` | genuine bug | The translator implements the negated property set with attribute scans only, so it misses the allowed ex:p3 object-property edge. |
| `property-path/pp16` | genuine bug | The translator seeds zero-length paths only from document URIs, omitting the explicit literal node "test" from the graph's term set. |
| `property-path/pp34` | genuine bug | The live harness does not load any manifest qt:graphData files, so the selected named graph and its zero-or-more path are empty. |
| `property-path/pp35` | genuine bug | The live harness does not load any manifest qt:graphData files, so the variable named graph path has no data to match. |
| `subquery/subquery01` | genuine bug | The live harness does not load the manifest's qt:graphData file, so the named graph surrounding the subquery is empty. |
| `subquery/subquery02` | genuine bug | The live harness does not load the manifest's qt:graphData file, so the named graph required to correlate ?g is empty. |
| `subquery/subquery03` | genuine bug | The live harness does not load the manifest's qt:graphData file, so the named graph surrounding the projected subquery is empty. |
| `subquery/subquery04` | genuine bug | The live harness loads only the default decoy graph and omits qt:graphData, so it returns :no instead of the two named-graph subjects. |
| `subquery/subquery05` | genuine bug | The live harness does not load the manifest's qt:graphData file, so the named graph surrounding the subquery is empty. |
| `subquery/subquery06` | genuine bug | The translator emits only attribute scans for the subquery's variable-predicate triple, omitting the loaded object-property edges. |
| `subquery/subquery07` | genuine bug | The live harness does not load the manifest's qt:graphData file, so the GRAPH pattern inside the subquery is empty. |

## Regeneration

```bash
RUN_INTEGRATION=1 W3C_STORAGE_PROFILE=document_edge \
  uv run python tests/w3c/analyze_coverage.py \
  --live --profile document_edge --write-live-failures
```
