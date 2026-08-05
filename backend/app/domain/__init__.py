"""Pure domain logic.

No I/O, no network, no database, no framework types. Everything here is a
function of its inputs, which is why it carries the heaviest test coverage in
the project: section parsing, chunking and citation handling are where a silent
regression becomes a wrong legal citation.
"""
