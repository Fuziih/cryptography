# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.


import ipaddress

from cryptography import utils, x509
from cryptography.hazmat.backends.openssl.decode_asn1 import (
    _DISTPOINT_TYPE_FULLNAME,
    _DISTPOINT_TYPE_RELATIVENAME,
)
from cryptography.x509.name import _ASN1Type
from cryptography.x509.oid import CRLEntryExtensionOID, ExtensionOID


def _encode_asn1_int(backend, x):
    """
    Converts a python integer to an ASN1_INTEGER. The returned ASN1_INTEGER
    will not be garbage collected (to support adding them to structs that take
    ownership of the object). Be sure to register it for GC if it will be
    discarded after use.

    """
    # Convert Python integer to OpenSSL "bignum" in case value exceeds
    # machine's native integer limits (note: `int_to_bn` doesn't automatically
    # GC).
    i = backend._int_to_bn(x)
    i = backend._ffi.gc(i, backend._lib.BN_free)

    # Wrap in an ASN.1 integer.  Don't GC -- as documented.
    i = backend._lib.BN_to_ASN1_INTEGER(i, backend._ffi.NULL)
    backend.openssl_assert(i != backend._ffi.NULL)
    return i


def _encode_asn1_int_gc(backend, x):
    i = _encode_asn1_int(backend, x)
    i = backend._ffi.gc(i, backend._lib.ASN1_INTEGER_free)
    return i


def _encode_asn1_str(backend, data):
    """
    Create an ASN1_OCTET_STRING from a Python byte string.
    """
    s = backend._lib.ASN1_OCTET_STRING_new()
    res = backend._lib.ASN1_OCTET_STRING_set(s, data, len(data))
    backend.openssl_assert(res == 1)
    return s


def _encode_asn1_str_gc(backend, data):
    s = _encode_asn1_str(backend, data)
    s = backend._ffi.gc(s, backend._lib.ASN1_OCTET_STRING_free)
    return s


def _encode_name(backend, name):
    """
    The X509_NAME created will not be gc'd. Use _encode_name_gc if needed.
    """
    subject = backend._lib.X509_NAME_new()
    for rdn in name.rdns:
        set_flag = 0  # indicate whether to add to last RDN or create new RDN
        for attribute in rdn:
            name_entry = _encode_name_entry(backend, attribute)
            # X509_NAME_add_entry dups the object so we need to gc this copy
            name_entry = backend._ffi.gc(
                name_entry, backend._lib.X509_NAME_ENTRY_free
            )
            res = backend._lib.X509_NAME_add_entry(
                subject, name_entry, -1, set_flag
            )
            backend.openssl_assert(res == 1)
            set_flag = -1
    return subject


def _encode_name_gc(backend, attributes):
    subject = _encode_name(backend, attributes)
    subject = backend._ffi.gc(subject, backend._lib.X509_NAME_free)
    return subject


def _encode_sk_name_entry(backend, attributes):
    """
    The sk_X509_NAME_ENTRY created will not be gc'd.
    """
    stack = backend._lib.sk_X509_NAME_ENTRY_new_null()
    for attribute in attributes:
        name_entry = _encode_name_entry(backend, attribute)
        res = backend._lib.sk_X509_NAME_ENTRY_push(stack, name_entry)
        backend.openssl_assert(res >= 1)
    return stack


def _encode_name_entry(backend, attribute):
    if attribute._type is _ASN1Type.BMPString:
        value = attribute.value.encode("utf_16_be")
    elif attribute._type is _ASN1Type.UniversalString:
        value = attribute.value.encode("utf_32_be")
    else:
        value = attribute.value.encode("utf8")

    obj = _txt2obj_gc(backend, attribute.oid.dotted_string)

    name_entry = backend._lib.X509_NAME_ENTRY_create_by_OBJ(
        backend._ffi.NULL, obj, attribute._type.value, value, len(value)
    )
    return name_entry


def _encode_issuing_dist_point(backend, ext):
    idp = backend._lib.ISSUING_DIST_POINT_new()
    backend.openssl_assert(idp != backend._ffi.NULL)
    idp = backend._ffi.gc(idp, backend._lib.ISSUING_DIST_POINT_free)
    idp.onlyuser = 255 if ext.only_contains_user_certs else 0
    idp.onlyCA = 255 if ext.only_contains_ca_certs else 0
    idp.indirectCRL = 255 if ext.indirect_crl else 0
    idp.onlyattr = 255 if ext.only_contains_attribute_certs else 0
    if ext.only_some_reasons:
        idp.onlysomereasons = _encode_reasonflags(
            backend, ext.only_some_reasons
        )

    if ext.full_name:
        idp.distpoint = _encode_full_name(backend, ext.full_name)

    if ext.relative_name:
        idp.distpoint = _encode_relative_name(backend, ext.relative_name)

    return idp


def _txt2obj(backend, name):
    """
    Converts a Python string with an ASN.1 object ID in dotted form to a
    ASN1_OBJECT.
    """
    name = name.encode("ascii")
    obj = backend._lib.OBJ_txt2obj(name, 1)
    backend.openssl_assert(obj != backend._ffi.NULL)
    return obj


def _txt2obj_gc(backend, name):
    obj = _txt2obj(backend, name)
    obj = backend._ffi.gc(obj, backend._lib.ASN1_OBJECT_free)
    return obj


def _encode_authority_key_identifier(backend, authority_keyid):
    akid = backend._lib.AUTHORITY_KEYID_new()
    backend.openssl_assert(akid != backend._ffi.NULL)
    akid = backend._ffi.gc(akid, backend._lib.AUTHORITY_KEYID_free)
    if authority_keyid.key_identifier is not None:
        akid.keyid = _encode_asn1_str(
            backend,
            authority_keyid.key_identifier,
        )

    if authority_keyid.authority_cert_issuer is not None:
        akid.issuer = _encode_general_names(
            backend, authority_keyid.authority_cert_issuer
        )

    if authority_keyid.authority_cert_serial_number is not None:
        akid.serial = _encode_asn1_int(
            backend, authority_keyid.authority_cert_serial_number
        )

    return akid


def _encode_information_access(backend, info_access):
    aia = backend._lib.sk_ACCESS_DESCRIPTION_new_null()
    backend.openssl_assert(aia != backend._ffi.NULL)
    aia = backend._ffi.gc(
        aia,
        lambda x: backend._lib.sk_ACCESS_DESCRIPTION_pop_free(
            x,
            backend._ffi.addressof(
                backend._lib._original_lib, "ACCESS_DESCRIPTION_free"
            ),
        ),
    )
    for access_description in info_access:
        ad = backend._lib.ACCESS_DESCRIPTION_new()
        method = _txt2obj(
            backend, access_description.access_method.dotted_string
        )
        _encode_general_name_preallocated(
            backend, access_description.access_location, ad.location
        )
        ad.method = method
        res = backend._lib.sk_ACCESS_DESCRIPTION_push(aia, ad)
        backend.openssl_assert(res >= 1)

    return aia


def _encode_general_names(backend, names):
    general_names = backend._lib.GENERAL_NAMES_new()
    backend.openssl_assert(general_names != backend._ffi.NULL)
    for name in names:
        gn = _encode_general_name(backend, name)
        res = backend._lib.sk_GENERAL_NAME_push(general_names, gn)
        backend.openssl_assert(res != 0)

    return general_names


def _encode_alt_name(backend, san):
    general_names = _encode_general_names(backend, san)
    general_names = backend._ffi.gc(
        general_names, backend._lib.GENERAL_NAMES_free
    )
    return general_names


def _encode_general_name(backend, name):
    gn = backend._lib.GENERAL_NAME_new()
    _encode_general_name_preallocated(backend, name, gn)
    return gn


def _encode_general_name_preallocated(backend, name, gn):
    if isinstance(name, x509.DNSName):
        backend.openssl_assert(gn != backend._ffi.NULL)
        gn.type = backend._lib.GEN_DNS

        ia5 = backend._lib.ASN1_IA5STRING_new()
        backend.openssl_assert(ia5 != backend._ffi.NULL)
        # ia5strings are supposed to be ITU T.50 but to allow round-tripping
        # of broken certs that encode utf8 we'll encode utf8 here too.
        value = name.value.encode("utf8")

        res = backend._lib.ASN1_STRING_set(ia5, value, len(value))
        backend.openssl_assert(res == 1)
        gn.d.dNSName = ia5
    elif isinstance(name, x509.RegisteredID):
        backend.openssl_assert(gn != backend._ffi.NULL)
        gn.type = backend._lib.GEN_RID
        obj = backend._lib.OBJ_txt2obj(
            name.value.dotted_string.encode("ascii"), 1
        )
        backend.openssl_assert(obj != backend._ffi.NULL)
        gn.d.registeredID = obj
    elif isinstance(name, x509.DirectoryName):
        backend.openssl_assert(gn != backend._ffi.NULL)
        dir_name = _encode_name(backend, name.value)
        gn.type = backend._lib.GEN_DIRNAME
        gn.d.directoryName = dir_name
    elif isinstance(name, x509.IPAddress):
        backend.openssl_assert(gn != backend._ffi.NULL)
        if isinstance(name.value, ipaddress.IPv4Network):
            packed = name.value.network_address.packed + utils.int_to_bytes(
                ((1 << 32) - name.value.num_addresses), 4
            )
        elif isinstance(name.value, ipaddress.IPv6Network):
            packed = name.value.network_address.packed + utils.int_to_bytes(
                (1 << 128) - name.value.num_addresses, 16
            )
        else:
            packed = name.value.packed
        ipaddr = _encode_asn1_str(backend, packed)
        gn.type = backend._lib.GEN_IPADD
        gn.d.iPAddress = ipaddr
    elif isinstance(name, x509.OtherName):
        backend.openssl_assert(gn != backend._ffi.NULL)
        other_name = backend._lib.OTHERNAME_new()
        backend.openssl_assert(other_name != backend._ffi.NULL)

        type_id = backend._lib.OBJ_txt2obj(
            name.type_id.dotted_string.encode("ascii"), 1
        )
        backend.openssl_assert(type_id != backend._ffi.NULL)
        data = backend._ffi.new("unsigned char[]", name.value)
        data_ptr_ptr = backend._ffi.new("unsigned char **")
        data_ptr_ptr[0] = data
        value = backend._lib.d2i_ASN1_TYPE(
            backend._ffi.NULL, data_ptr_ptr, len(name.value)
        )
        if value == backend._ffi.NULL:
            backend._consume_errors()
            raise ValueError("Invalid ASN.1 data")
        other_name.type_id = type_id
        other_name.value = value
        gn.type = backend._lib.GEN_OTHERNAME
        gn.d.otherName = other_name
    elif isinstance(name, x509.RFC822Name):
        backend.openssl_assert(gn != backend._ffi.NULL)
        # ia5strings are supposed to be ITU T.50 but to allow round-tripping
        # of broken certs that encode utf8 we'll encode utf8 here too.
        data = name.value.encode("utf8")
        asn1_str = _encode_asn1_str(backend, data)
        gn.type = backend._lib.GEN_EMAIL
        gn.d.rfc822Name = asn1_str
    elif isinstance(name, x509.UniformResourceIdentifier):
        backend.openssl_assert(gn != backend._ffi.NULL)
        # ia5strings are supposed to be ITU T.50 but to allow round-tripping
        # of broken certs that encode utf8 we'll encode utf8 here too.
        data = name.value.encode("utf8")
        asn1_str = _encode_asn1_str(backend, data)
        gn.type = backend._lib.GEN_URI
        gn.d.uniformResourceIdentifier = asn1_str
    else:
        raise ValueError("{} is an unknown GeneralName type".format(name))


_CRLREASONFLAGS = {
    x509.ReasonFlags.key_compromise: 1,
    x509.ReasonFlags.ca_compromise: 2,
    x509.ReasonFlags.affiliation_changed: 3,
    x509.ReasonFlags.superseded: 4,
    x509.ReasonFlags.cessation_of_operation: 5,
    x509.ReasonFlags.certificate_hold: 6,
    x509.ReasonFlags.privilege_withdrawn: 7,
    x509.ReasonFlags.aa_compromise: 8,
}


def _encode_reasonflags(backend, reasons):
    bitmask = backend._lib.ASN1_BIT_STRING_new()
    backend.openssl_assert(bitmask != backend._ffi.NULL)
    for reason in reasons:
        res = backend._lib.ASN1_BIT_STRING_set_bit(
            bitmask, _CRLREASONFLAGS[reason], 1
        )
        backend.openssl_assert(res == 1)

    return bitmask


def _encode_full_name(backend, full_name):
    dpn = backend._lib.DIST_POINT_NAME_new()
    backend.openssl_assert(dpn != backend._ffi.NULL)
    dpn.type = _DISTPOINT_TYPE_FULLNAME
    dpn.name.fullname = _encode_general_names(backend, full_name)
    return dpn


def _encode_relative_name(backend, relative_name):
    dpn = backend._lib.DIST_POINT_NAME_new()
    backend.openssl_assert(dpn != backend._ffi.NULL)
    dpn.type = _DISTPOINT_TYPE_RELATIVENAME
    dpn.name.relativename = _encode_sk_name_entry(backend, relative_name)
    return dpn


def _encode_cdps_freshest_crl(backend, cdps):
    cdp = backend._lib.sk_DIST_POINT_new_null()
    cdp = backend._ffi.gc(cdp, backend._lib.sk_DIST_POINT_free)
    for point in cdps:
        dp = backend._lib.DIST_POINT_new()
        backend.openssl_assert(dp != backend._ffi.NULL)

        if point.reasons:
            dp.reasons = _encode_reasonflags(backend, point.reasons)

        if point.full_name:
            dp.distpoint = _encode_full_name(backend, point.full_name)

        if point.relative_name:
            dp.distpoint = _encode_relative_name(backend, point.relative_name)

        if point.crl_issuer:
            dp.CRLissuer = _encode_general_names(backend, point.crl_issuer)

        res = backend._lib.sk_DIST_POINT_push(cdp, dp)
        backend.openssl_assert(res >= 1)

    return cdp


def _encode_name_constraints(backend, name_constraints):
    nc = backend._lib.NAME_CONSTRAINTS_new()
    backend.openssl_assert(nc != backend._ffi.NULL)
    nc = backend._ffi.gc(nc, backend._lib.NAME_CONSTRAINTS_free)
    permitted = _encode_general_subtree(
        backend, name_constraints.permitted_subtrees
    )
    nc.permittedSubtrees = permitted
    excluded = _encode_general_subtree(
        backend, name_constraints.excluded_subtrees
    )
    nc.excludedSubtrees = excluded

    return nc


def _encode_general_subtree(backend, subtrees):
    if subtrees is None:
        return backend._ffi.NULL
    else:
        general_subtrees = backend._lib.sk_GENERAL_SUBTREE_new_null()
        for name in subtrees:
            gs = backend._lib.GENERAL_SUBTREE_new()
            gs.base = _encode_general_name(backend, name)
            res = backend._lib.sk_GENERAL_SUBTREE_push(general_subtrees, gs)
            backend.openssl_assert(res >= 1)

        return general_subtrees


_EXTENSION_ENCODE_HANDLERS = {
    ExtensionOID.SUBJECT_ALTERNATIVE_NAME: _encode_alt_name,
    ExtensionOID.ISSUER_ALTERNATIVE_NAME: _encode_alt_name,
    ExtensionOID.AUTHORITY_KEY_IDENTIFIER: _encode_authority_key_identifier,
    ExtensionOID.AUTHORITY_INFORMATION_ACCESS: _encode_information_access,
    ExtensionOID.SUBJECT_INFORMATION_ACCESS: _encode_information_access,
    ExtensionOID.CRL_DISTRIBUTION_POINTS: _encode_cdps_freshest_crl,
    ExtensionOID.FRESHEST_CRL: _encode_cdps_freshest_crl,
    ExtensionOID.NAME_CONSTRAINTS: _encode_name_constraints,
}

_CRL_EXTENSION_ENCODE_HANDLERS = {
    ExtensionOID.ISSUER_ALTERNATIVE_NAME: _encode_alt_name,
    ExtensionOID.AUTHORITY_KEY_IDENTIFIER: _encode_authority_key_identifier,
    ExtensionOID.AUTHORITY_INFORMATION_ACCESS: _encode_information_access,
    ExtensionOID.ISSUING_DISTRIBUTION_POINT: _encode_issuing_dist_point,
    ExtensionOID.FRESHEST_CRL: _encode_cdps_freshest_crl,
}

_CRL_ENTRY_EXTENSION_ENCODE_HANDLERS = {
    CRLEntryExtensionOID.CERTIFICATE_ISSUER: _encode_alt_name,
}
