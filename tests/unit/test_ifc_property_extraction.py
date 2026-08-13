import pytest

ifcopenshell = pytest.importorskip("ifcopenshell")

from speckleifc.property_extraction import extract_properties  # noqa: E402


def _guid() -> str:
    return ifcopenshell.guid.new()


@pytest.fixture
def ifc_file():
    return ifcopenshell.file(schema="IFC4")


def _add_uniclass(file, identification="Ss_25_10_30"):
    """A hierarchical Uniclass reference chain, as real files encode it."""
    system = file.create_entity(
        "IfcClassification", Source="NBS", Edition="2015", Name="Uniclass 2015"
    )
    parent = file.create_entity(
        "IfcClassificationReference",
        Identification="Ss_25",
        Name="Wall and barrier systems",
        ReferencedSource=system,
    )
    return file.create_entity(
        "IfcClassificationReference",
        Location="https://uniclass.thenbs.com/Ss_25_10_30",
        Identification=identification,
        Name="Cast in situ concrete wall systems",
        ReferencedSource=parent,
    )


def _associate(file, element, reference):
    file.create_entity(
        "IfcRelAssociatesClassification",
        GlobalId=_guid(),
        RelatedObjects=[element],
        RelatingClassification=reference,
    )


def test_extracts_classification_keyed_by_system(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate(ifc_file, wall, _add_uniclass(ifc_file))

    classifications = extract_properties(wall)["Classifications"]

    assert classifications == {
        "Uniclass 2015": {
            "Identification": "Ss_25_10_30",
            "Name": "Cast in situ concrete wall systems",
            "Location": "https://uniclass.thenbs.com/Ss_25_10_30",
        }
    }


def test_extracts_multiple_systems(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate(ifc_file, wall, _add_uniclass(ifc_file))

    ccs = ifc_file.create_entity("IfcClassification", Source="Molio", Name="CCS")
    _associate(
        ifc_file,
        wall,
        ifc_file.create_entity(
            "IfcClassificationReference",
            Identification="BA",
            Name="Groundworks structure",
            ReferencedSource=ccs,
        ),
    )

    classifications = extract_properties(wall)["Classifications"]

    assert set(classifications) == {"Uniclass 2015", "CCS"}
    assert classifications["CCS"]["Identification"] == "BA"


def test_inherits_classification_from_element_type(ifc_file):
    slab_type = ifc_file.create_entity(
        "IfcSlabType", GlobalId=_guid(), Name="SL_Concrete220"
    )
    slab = ifc_file.create_entity("IfcSlab", GlobalId=_guid(), Name="Slab-001")
    ifc_file.create_entity(
        "IfcRelDefinesByType",
        GlobalId=_guid(),
        RelatedObjects=[slab],
        RelatingType=slab_type,
    )
    _associate(ifc_file, slab_type, _add_uniclass(ifc_file))

    classifications = extract_properties(slab)["Classifications"]

    assert classifications["Uniclass 2015"]["Identification"] == "Ss_25_10_30"


def test_unclassified_element_has_no_classifications_key(ifc_file):
    beam = ifc_file.create_entity("IfcBeam", GlobalId=_guid(), Name="Beam-001")

    assert "Classifications" not in extract_properties(beam)


def _sourceless_reference(file):
    """
    ReferencedSource is optional. A real ArchiCAD export used sourceless
    references for Finnish area categories on IfcSpace - not classification
    systems at all.
    """
    return file.create_entity(
        "IfcClassificationReference",
        Identification="9350",
        Name="Huoneala",
        ReferencedSource=None,
    )


def test_reference_without_a_source_system_is_skipped(ifc_file):
    space = ifc_file.create_entity("IfcSpace", GlobalId=_guid(), Name="Space-001")
    _associate(ifc_file, space, _sourceless_reference(ifc_file))

    assert "Classifications" not in extract_properties(space)


def test_sourceless_reference_does_not_displace_a_real_system(ifc_file):
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    _associate(ifc_file, wall, _add_uniclass(ifc_file))
    _associate(ifc_file, wall, _sourceless_reference(ifc_file))

    assert set(extract_properties(wall)["Classifications"]) == {"Uniclass 2015"}


@pytest.mark.parametrize(
    "association_order",
    [("Ss_25_10_30", "Ss_25_10_95"), ("Ss_25_10_95", "Ss_25_10_30")],
)
def test_duplicate_system_keeps_lowest_identification(ifc_file, association_order):
    """
    One entry per system, and which reference wins does not depend on the order
    they were associated in (`get_references` returns a set, whose iteration
    order is incidental rather than guaranteed).
    """
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    for identification in association_order:
        _associate(ifc_file, wall, _add_uniclass(ifc_file, identification))

    classifications = extract_properties(wall)["Classifications"]

    assert classifications["Uniclass 2015"]["Identification"] == "Ss_25_10_30"


def test_leaves_existing_property_extraction_untouched(ifc_file):
    """Classifications are additive: nothing else about the payload changes."""
    wall = ifc_file.create_entity("IfcWall", GlobalId=_guid(), Name="Wall-001")
    before = set(extract_properties(wall))

    _associate(ifc_file, wall, _add_uniclass(ifc_file))

    assert set(extract_properties(wall)) == before | {"Classifications"}
