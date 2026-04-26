from dataset_citation_finder import (
    find_citations,
    find_citations_batch,
    find_citations_by_shoulder,
    collapse_sources,
)

# Single DOI
df = find_citations("10.6073/pasta/925c86b094c123fae4ddabf31cf92a30 ", email="gmaurer@nmsu.edu")
print(df)
