#!/usr/bin/env python3
# coding: utf8
import sys
import argparse
import datetime
import os
import re
import shutil
from collections import OrderedDict
from Bio import SeqFeature as sf
from Bio import SeqIO
from Bio.Alphabet import IUPAC
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from eggnog2gbk.utils import is_valid_file, create_GO_dataframes, read_annotation, create_taxonomic_data


"""
Description:
Using fasta files (scaffold/chromosme/contig file, protein file), annotation tsv file from eggnog and the species name
this script writes a genbank file with EC number and Go annotations.
The species name needs to be compatible with the taxonomy of the EBI.
Informations need a good formating:
gene ID should be correctly written (like XXX_001 and no XXX_1 if you got more thant 100 genes).
Currently when there is multiple GO terms/EC the script split them when they are separated by ";" or by "," like GO:0006979;GO:0020037;GO:0004601,
if you use another separator add to the re.split(',|;').
Other informations can be added by adding a dictionary with gene ID as key and the information
as value and adapt the condition used for the others annotations (EC, Go term).
"""

def contig_info(contig_id, contig_seq, species_informations):
    """
    Create contig information from species_informations dictionary and contig id and contig seq.
    """
    record = SeqRecord(contig_seq, id=contig_id, name=contig_id,
                    description=species_informations['description'])

    record.seq.alphabet = IUPAC.ambiguous_dna
    if 'data_file_division' in species_informations:
        record.annotations['data_file_division'] = species_informations['data_file_division']
    record.annotations['date'] = datetime.date.today().strftime('%d-%b-%Y').upper()
    if 'topology' in species_informations:
        record.annotations['topology'] = species_informations['topology']
    record.annotations['accessions'] = contig_id
    if 'organism' in species_informations:
        record.annotations['organism'] = species_informations['organism']
    # Use of literal_eval for taxonomy and keywords to retrieve list.
    if 'taxonomy' in species_informations:
        record.annotations['taxonomy'] = species_informations['taxonomy']
    if 'keywords' in species_informations:
        record.annotations['keywords'] = species_informations['keywords']
    if 'source' in species_informations:
        record.annotations['source'] = species_informations['source']

    new_feature_source = sf.SeqFeature(sf.FeatureLocation(1-1,
                                                        len(contig_seq)),
                                                        type="source")
    new_feature_source.qualifiers['scaffold'] = contig_id
    if 'isolate' in species_informations:
        new_feature_source.qualifiers['isolate'] = species_informations['isolate']
    # db_xref corresponds to the taxon NCBI ID.
    # Important if you want to use Pathway Tools after.
    if 'db_xref' in species_informations:
        new_feature_source.qualifiers['db_xref'] = species_informations['db_xref']
    if 'cell_type' in species_informations:
        new_feature_source.qualifiers['cell_type'] = species_informations['cell_type']
    if 'dev_stage' in species_informations:
        new_feature_source.qualifiers['dev_stage'] = species_informations['dev_stage']
    if 'mol_type' in species_informations:
        new_feature_source.qualifiers['mol_type'] = species_informations['mol_type']

    record.features.append(new_feature_source)

    return record

def strand_change(input_strand):
    """
    The input is strand in str ('-', '+') modify it to be a strand in int (-1, +1) to 
    be compatible with SeqIO strand reading.
    """
    if isinstance(input_strand, str):
        if input_strand == '-':
            new_strand = -1
        elif input_strand == '+':
            new_strand = +1
        if input_strand == '.':
            new_strand = None
        elif input_strand == '?':
            new_strand = 0
    elif isinstance(input_strand, int):
        if input_strand == -1:
            new_strand = input_strand
        elif input_strand == +1:
            new_strand = input_strand

    return new_strand

def search_and_add_RNA(gff_database, gene_informations, record, type_RNA):
    """
    Search in the gff_database if the gene have RNA of the (type_RNA).
    For the RNA it will add a feature to the contig record of the genbank.
    Then it returns the contig record.
    gene_informations contain:
        [0] -> gene feature
        [1] -> gene ID cleaned
        [2] -> gene start position
        [3] -> gene end postion
        [4] -> gene strand modified (str -> int)
    """
    for rna in gff_database.children(gene_informations[0], featuretype=type_RNA, order_by='start'):
        new_feature_RNA = sf.SeqFeature(sf.FeatureLocation(gene_informations[2],
                                                            gene_informations[3],
                                                            gene_informations[4]),
                                                            type=type_RNA)
        new_feature_RNA.qualifiers['locus_tag'] = gene_informations[1]
        record.features.append(new_feature_RNA)
    return record

def search_and_add_pseudogene(gff_database, gene, record, df_exons, gene_protein_seq):
    """
    Search in the gff_database if the gene is a pseudogene.
    Add it to the record.
    """
    location_exons = []

    for pseudogene in gff_database.children(gene, featuretype="pseudogene", order_by='start'):
        # Select exon corresponding to the gene.
        # Then iterate for each exon and extract information.
        df_temp = df_exons[df_exons['gene_id'] == pseudogene.id]
        for _, row in df_temp.iterrows():
            new_feature_location_exons = sf.FeatureLocation(row['start'],
                                                            row['end'],
                                                            row['strand'])
            location_exons.append(new_feature_location_exons)
        if location_exons and len(location_exons)>=2:
            exon_compound_locations = sf.CompoundLocation(location_exons, operator='join')

            new_feature_cds = sf.SeqFeature(exon_compound_locations, type='CDS')
        else:
            start_position = gene.start -1
            end_position = gene.end
            strand = strand_change(gene.strand)
            new_feature_cds = sf.SeqFeature(sf.FeatureLocation(start_position,
                                                                end_position,
                                                                strand),
                                                            type="CDS")

        new_feature_cds.qualifiers['translation'] = gene_protein_seq[pseudogene.id]
        new_feature_cds.qualifiers['locus_tag'] = gene.id + "_0002"
        new_feature_cds.qualifiers['pseudo'] = None
        record.features.append(new_feature_cds)
    return record

def faa_to_gbk(genome_fasta, prot_fasta, annot_table, species_name, gbk_out, gobasic=None):
    """
    From a genome fasta (containing each contigs of the genome),
    a protein fasta (containing each protein sequence),
    an annotation table (containing gene name associated with GO terms, InterPro and EC),
    a gff file (containing gene, exon, mRNA, ncRNA, tRNA),
    a contig information table (containing species name, taxon ID, ..)
    create a genbank file.
    """
    print('Formatting fasta and annotation file')
    # Dictionary with scaffold/chromosome id as key and sequence as value.
    contig_seqs = OrderedDict()

    for record in SeqIO.parse(genome_fasta, "fasta"):
        id_contig = record.id
        contig_seqs[id_contig] = record.seq


    # Dictionary with gene id as key and protein sequence as value.
    gene_protein_seq = {}

    for record in SeqIO.parse(prot_fasta, "fasta"):
        gene_protein_seq[record.id] = record.seq

    # Create a taxonomy dictionary querying the EBI.
    species_informations = create_taxonomic_data(species_name)

    # Read the ggnog tsv file containing GO terms and EC associated with gene name.
    annotation_data = read_annotation(annot_table)

    # Query Gene Ontology to extract namespaces and alternative IDs.
    df_go_namespace, df_go_alternative = create_GO_dataframes(gobasic)
    # Dictionary GO id as term and GO namespace as value.
    df_go_namespace.set_index('GO', inplace=True)
    go_namespaces = df_go_namespace['namespace'].to_dict()

    # Dictionary GO id as term and GO alternatives id as value.
    df_go_alternative.set_index('GO', inplace=True)
    go_alternatives = df_go_alternative['alternative_GO'].to_dict()

    # All SeqRecord objects will be stored in a list and then give to the SeqIO writer to create the genbank.
    seq_objects = []

    print('Assembling Genbank informations')

    # Iterate through each contig.
    # Then iterate through gene and throug RNA linked with the gene.
    # Then look if protein informations are available.
    for contig_id in sorted(contig_seqs):
        # Data for each contig.
        record = contig_info(contig_id, contig_seqs[contig_id], species_informations)
        # if id is numeric, change it
        if contig_id.isnumeric():
            id_gene = f("gene_{contig_id}")
        else:
            id_gene = contig_id
        start_position = 1
        end_position = len(contig_seqs[contig_id])
        strand = 0
        new_feature_gene = sf.SeqFeature(sf.FeatureLocation(start_position,
                                                            end_position,
                                                            strand),
                                                            #),
                                                            type="gene")
        new_feature_gene.qualifiers['locus_tag'] = id_gene # + "_0001"
        # print(new_feature_gene.qualifiers['locus_tag'] )
        # Add gene information to contig record.
        record.features.append(new_feature_gene)

        location_exons = []

        new_feature_cds = sf.SeqFeature(sf.FeatureLocation(start_position,
                                                                end_position,
                                                                # strand),
                                                                ),
                                                            type="CDS")

        new_feature_cds.qualifiers['translation'] = gene_protein_seq[contig_id] #ad_gene
        new_feature_cds.qualifiers['locus_tag'] = id_gene # + "_0001"

        # Add GO annotation according to the namespace.
        # print(contig_id)
        if contig_id in annotation_data.keys():
            gene_gos = re.split(';|,', annotation_data[contig_id]['GOs'])
            if gene_gos != [""]:
                go_components = []
                go_functions = []
                go_process = []

                for go in gene_gos:
                    # Check if GO term is not a deprecated one.
                    # If yes take the corresponding one in alternative GO.
                    if go not in go_namespaces:
                        go_test = go_alternatives[go]
                    else:
                        go_test = go
                    if go_namespaces[go_test] == 'cellular_component':
                            go_components.append(go)
                    if go_namespaces[go_test] == 'molecular_function':
                        go_functions.append(go)
                    if go_namespaces[go_test] == 'biological_process':
                        go_process.append(go)                           
                new_feature_cds.qualifiers['go_component'] = go_components
                new_feature_cds.qualifiers['go_function'] = go_functions
                new_feature_cds.qualifiers['go_process'] = go_process

            # Add EC annotation.
        if contig_id in annotation_data.keys():
            gene_ecs = re.split(';|,', annotation_data[contig_id]['EC'])
            if gene_ecs != [""]:
                new_feature_cds.qualifiers['EC_number'] = [ec.replace('ec:', '') for ec in gene_ecs]

        # Add CDS information to contig record
        record.features.append(new_feature_cds)
        # print(record.features)
        seq_objects.append(record)

    # Create Genbank with the list of SeqRecord.
    SeqIO.write(seq_objects, gbk_out, 'genbank')

def main(genome_fasta, prot_fasta, annot_table, species_name, gbk_out, gobasic_file = None):
    # check validity of inputs
    for elem in [genome_fasta, prot_fasta, annot_table]:
        if not is_valid_file(elem):
            print(f"{elem} is not a valid path file.")
            sys.exit(1)
    if gobasic_file:
        if not is_valid_file(gobasic_file):
            print(f"{gobasic_file} is not a valid path file.")
            sys.exit(1)


    faa_to_gbk(genome_fasta, prot_fasta, annot_table, species_name, gbk_out, gobasic_file)