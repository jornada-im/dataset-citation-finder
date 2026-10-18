from dataset_citation_finder import find_citations

# Single DOI
df = find_citations("10.6073/pasta/925c86b094c123fae4ddabf31cf92a30 ", email="gmaurer@nmsu.edu")
print(df)
