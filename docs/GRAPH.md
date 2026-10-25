# Knowledge graph

Items are the nodes; typed, directed edges link them (`kb_link`, `kb_unlink`, `kb_graph`).
Suggested relations: `related`, `part_of`, `next`, `derived_from`, `supports`, `contradicts`,
`supersedes`, `mentions` (any short lowercase word is accepted).

- **Creating a link** needs write access to the source item and read access to the target. So a user can
  record that their note `derived_from` another group's (external) finding.
- **Seeing a link** needs read access to *both* ends; traversal only walks through items the caller can see,
  so a link never reveals an item the caller cannot see. Moving an item (promotion/demotion) changes who sees
  its links automatically; archived items are hidden from the graph.
- Traversal is 1-3 hops, capped at 100 nodes (`truncated` says when more exist).
- Removal: the link's author, or an editor of the source item. Changes are audited (`edge.add`, `edge.remove`).
