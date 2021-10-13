// This file is dual licensed under the terms of the Apache License, Version
// 2.0, and the BSD License. See the LICENSE file in the root of this repository
// for complete details.

use crate::asn1::PyAsn1Error;
use chrono::{Datelike, TimeZone, Timelike};
use pyo3::ToPyObject;
use std::collections::HashSet;
use std::convert::TryInto;
use std::marker::PhantomData;

/// parse all sections in a PEM file and return the only matching section.
/// If no or multiple matching sections are found, return an error.
pub(crate) fn find_in_pem(
    data: &[u8],
    filter_fn: fn(&pem::Pem) -> bool,
    no_match_err: &'static str,
    multiple_match_err: &'static str,
) -> Result<pem::Pem, PyAsn1Error> {
    let all_sections = pem::parse_many(data)?;
    if all_sections.is_empty() {
        return Err(PyAsn1Error::from(pem::PemError::MalformedFraming));
    }
    let matching_sections: Vec<pem::Pem> = all_sections.into_iter().filter(filter_fn).collect();
    if matching_sections.len() > 1 {
        return Err(PyAsn1Error::from(pyo3::exceptions::PyValueError::new_err(
            multiple_match_err,
        )));
    }
    matching_sections
        .into_iter()
        .next()
        .ok_or_else(|| PyAsn1Error::from(pyo3::exceptions::PyValueError::new_err(no_match_err)))
}

pub(crate) type Name<'a> = asn1::SequenceOf<'a, asn1::SetOf<'a, AttributeTypeValue<'a>>>;

#[derive(asn1::Asn1Read, asn1::Asn1Write, PartialEq, Hash)]
pub(crate) struct AttributeTypeValue<'a> {
    pub(crate) type_id: asn1::ObjectIdentifier<'a>,
    pub(crate) value: asn1::Tlv<'a>,
}

pub(crate) struct UnvalidatedIA5String<'a>(&'a str);

impl<'a> asn1::SimpleAsn1Readable<'a> for UnvalidatedIA5String<'a> {
    const TAG: u8 = 0x16;
    fn parse_data(data: &'a [u8]) -> asn1::ParseResult<Self> {
        Ok(UnvalidatedIA5String(std::str::from_utf8(data).map_err(
            |_| asn1::ParseError::new(asn1::ParseErrorKind::InvalidValue),
        )?))
    }
}

#[derive(asn1::Asn1Read)]
pub(crate) enum GeneralName<'a> {
    #[implicit(0)]
    OtherName(AttributeTypeValue<'a>),

    #[implicit(1)]
    RFC822Name(UnvalidatedIA5String<'a>),

    #[implicit(2)]
    DNSName(UnvalidatedIA5String<'a>),

    #[implicit(3)]
    // unsupported
    X400Address(asn1::Sequence<'a>),

    // Name is explicit per RFC 5280 Appendix A.1.
    #[explicit(4)]
    DirectoryName(Name<'a>),

    #[implicit(5)]
    // unsupported
    EDIPartyName(asn1::Sequence<'a>),

    #[implicit(6)]
    UniformResourceIdentifier(UnvalidatedIA5String<'a>),

    #[implicit(7)]
    IPAddress(&'a [u8]),

    #[implicit(8)]
    RegisteredID(asn1::ObjectIdentifier<'a>),
}

#[derive(asn1::Asn1Read, asn1::Asn1Write, PartialEq, Hash, Clone)]
pub(crate) enum Time {
    UtcTime(asn1::UtcTime),
    GeneralizedTime(asn1::GeneralizedTime),
}

impl Time {
    pub(crate) fn as_chrono(&self) -> &chrono::DateTime<chrono::Utc> {
        match self {
            Time::UtcTime(data) => data.as_chrono(),
            Time::GeneralizedTime(data) => data.as_chrono(),
        }
    }
}

pub(crate) type Extensions<'a> = asn1::SequenceOf<'a, Extension<'a>>;

#[derive(asn1::Asn1Read, asn1::Asn1Write, PartialEq, Hash)]
pub(crate) struct AlgorithmIdentifier<'a> {
    pub(crate) oid: asn1::ObjectIdentifier<'a>,
    pub(crate) _params: Option<asn1::Tlv<'a>>,
}

#[derive(asn1::Asn1Read, asn1::Asn1Write, PartialEq, Hash)]
pub(crate) struct Extension<'a> {
    pub(crate) extn_id: asn1::ObjectIdentifier<'a>,
    #[default(false)]
    pub(crate) critical: bool,
    pub(crate) extn_value: &'a [u8],
}

pub(crate) fn parse_name<'p>(
    py: pyo3::Python<'p>,
    name: &Name<'_>,
) -> pyo3::PyResult<&'p pyo3::PyAny> {
    let x509_module = py.import("cryptography.x509")?;
    let py_rdns = pyo3::types::PyList::empty(py);
    for rdn in name.clone() {
        let py_rdn = parse_rdn(py, rdn)?;
        py_rdns.append(py_rdn)?;
    }
    x509_module.call_method1("Name", (py_rdns,))
}

fn parse_name_attribute(
    py: pyo3::Python<'_>,
    attribute: AttributeTypeValue<'_>,
) -> Result<pyo3::PyObject, PyAsn1Error> {
    let x509_module = py.import("cryptography.x509")?;
    let oid = x509_module
        .call_method1("ObjectIdentifier", (attribute.type_id.to_string(),))?
        .to_object(py);
    let tag_enum = py
        .import("cryptography.x509.name")?
        .getattr("_ASN1_TYPE_TO_ENUM")?;
    let py_tag = tag_enum.get_item(attribute.value.tag().to_object(py))?;
    let py_data = std::str::from_utf8(attribute.value.data())
        .map_err(|_| asn1::ParseError::new(asn1::ParseErrorKind::InvalidValue))?;
    Ok(x509_module
        .call_method1("NameAttribute", (oid, py_data, py_tag))?
        .to_object(py))
}

pub(crate) fn parse_rdn<'a>(
    py: pyo3::Python<'_>,
    rdn: asn1::SetOf<'a, AttributeTypeValue<'a>>,
) -> Result<pyo3::PyObject, PyAsn1Error> {
    let x509_module = py.import("cryptography.x509")?;
    let py_attrs = pyo3::types::PySet::empty(py)?;
    for attribute in rdn {
        let na = parse_name_attribute(py, attribute)?;
        py_attrs.add(na)?;
    }
    Ok(x509_module
        .call_method1("RelativeDistinguishedName", (py_attrs,))?
        .to_object(py))
}

pub(crate) fn parse_general_name(
    py: pyo3::Python<'_>,
    gn: GeneralName<'_>,
) -> Result<pyo3::PyObject, PyAsn1Error> {
    let x509_module = py.import("cryptography.x509")?;
    let py_gn = match gn {
        GeneralName::OtherName(data) => {
            let oid = x509_module
                .call_method1("ObjectIdentifier", (data.type_id.to_string(),))?
                .to_object(py);
            x509_module
                .call_method1("OtherName", (oid, data.value.data()))?
                .to_object(py)
        }
        GeneralName::RFC822Name(data) => x509_module
            .getattr("RFC822Name")?
            .call_method1("_init_without_validation", (data.0,))?
            .to_object(py),
        GeneralName::DNSName(data) => x509_module
            .getattr("DNSName")?
            .call_method1("_init_without_validation", (data.0,))?
            .to_object(py),
        GeneralName::DirectoryName(data) => {
            let py_name = parse_name(py, &data)?;
            x509_module
                .call_method1("DirectoryName", (py_name,))?
                .to_object(py)
        }
        GeneralName::UniformResourceIdentifier(data) => x509_module
            .getattr("UniformResourceIdentifier")?
            .call_method1("_init_without_validation", (data.0,))?
            .to_object(py),
        GeneralName::IPAddress(data) => {
            let ip_module = py.import("ipaddress")?;
            if data.len() == 4 || data.len() == 16 {
                let addr = ip_module.call_method1("ip_address", (data,))?.to_object(py);
                x509_module
                    .call_method1("IPAddress", (addr,))?
                    .to_object(py)
            } else {
                // if it's not an IPv4 or IPv6 we assume it's an IPNetwork and
                // verify length in this function.
                create_ip_network(py, data)?
            }
        }
        GeneralName::RegisteredID(data) => {
            let oid = x509_module
                .call_method1("ObjectIdentifier", (data.to_string(),))?
                .to_object(py);
            x509_module
                .call_method1("RegisteredID", (oid,))?
                .to_object(py)
        }
        _ => {
            return Err(PyAsn1Error::from(pyo3::PyErr::from_instance(
                x509_module.call_method1(
                    "UnsupportedGeneralNameType",
                    ("x400Address/EDIPartyName are not supported types",),
                )?,
            )))
        }
    };
    Ok(py_gn)
}

pub(crate) fn parse_general_names<'a>(
    py: pyo3::Python<'_>,
    gn_seq: asn1::SequenceOf<'a, GeneralName<'a>>,
) -> Result<pyo3::PyObject, PyAsn1Error> {
    let gns = pyo3::types::PyList::empty(py);
    for gn in gn_seq {
        let py_gn = parse_general_name(py, gn)?;
        gns.append(py_gn)?;
    }
    Ok(gns.to_object(py))
}

fn create_ip_network(py: pyo3::Python<'_>, data: &[u8]) -> Result<pyo3::PyObject, PyAsn1Error> {
    let ip_module = py.import("ipaddress")?;
    let x509_module = py.import("cryptography.x509")?;
    let prefix = match data.len() {
        8 => {
            let num = u32::from_be_bytes(data[4..].try_into().unwrap());
            ipv4_netmask(num)
        }
        32 => {
            let num = u128::from_be_bytes(data[16..].try_into().unwrap());
            ipv6_netmask(num)
        }
        _ => Err(PyAsn1Error::from(pyo3::exceptions::PyValueError::new_err(
            format!("Invalid IPNetwork, must be 8 bytes for IPv4 and 32 bytes for IPv6. Found length: {}", data.len()),
        ))),
    };
    let base = ip_module.call_method1(
        "ip_address",
        (pyo3::types::PyBytes::new(py, &data[..data.len() / 2]),),
    )?;
    let net = format!(
        "{}/{}",
        base.getattr("exploded")?.extract::<&str>()?,
        prefix?
    );
    let addr = ip_module.call_method1("ip_network", (net,))?.to_object(py);
    Ok(x509_module
        .call_method1("IPAddress", (addr,))?
        .to_object(py))
}

fn ipv4_netmask(num: u32) -> Result<u32, PyAsn1Error> {
    // we invert and check leading zeros because leading_ones wasn't stabilized
    // until 1.46.0. When we raise our MSRV we should change this
    if (!num).leading_zeros() + num.trailing_zeros() != 32 {
        return Err(PyAsn1Error::from(pyo3::exceptions::PyValueError::new_err(
            "Invalid netmask",
        )));
    }
    Ok((!num).leading_zeros())
}

fn ipv6_netmask(num: u128) -> Result<u32, PyAsn1Error> {
    // we invert and check leading zeros because leading_ones wasn't stabilized
    // until 1.46.0. When we raise our MSRV we should change this
    if (!num).leading_zeros() + num.trailing_zeros() != 128 {
        return Err(PyAsn1Error::from(pyo3::exceptions::PyValueError::new_err(
            "Invalid netmask",
        )));
    }
    Ok((!num).leading_zeros())
}

pub(crate) fn parse_and_cache_extensions<
    'p,
    F: Fn(&asn1::ObjectIdentifier<'_>, &[u8]) -> Result<Option<&'p pyo3::PyAny>, PyAsn1Error>,
>(
    py: pyo3::Python<'p>,
    cached_extensions: &mut Option<pyo3::PyObject>,
    raw_exts: &Option<Extensions<'_>>,
    parse_ext: F,
) -> pyo3::PyResult<pyo3::PyObject> {
    if let Some(cached) = cached_extensions {
        return Ok(cached.clone_ref(py));
    }

    let x509_module = py.import("cryptography.x509")?;
    let exts = pyo3::types::PyList::empty(py);
    let mut seen_oids = HashSet::new();
    if let Some(raw_exts) = raw_exts {
        for raw_ext in raw_exts.clone() {
            let oid_obj =
                x509_module.call_method1("ObjectIdentifier", (raw_ext.extn_id.to_string(),))?;

            if seen_oids.contains(&raw_ext.extn_id) {
                return Err(pyo3::PyErr::from_instance(x509_module.call_method1(
                    "DuplicateExtension",
                    (
                        format!("Duplicate {} extension found", raw_ext.extn_id),
                        oid_obj,
                    ),
                )?));
            }

            let extn_value = match parse_ext(&raw_ext.extn_id, raw_ext.extn_value)? {
                Some(e) => e,
                None => x509_module
                    .call_method1("UnrecognizedExtension", (oid_obj, raw_ext.extn_value))?,
            };
            let ext_obj =
                x509_module.call_method1("Extension", (oid_obj, raw_ext.critical, extn_value))?;
            exts.append(ext_obj)?;
            seen_oids.insert(raw_ext.extn_id);
        }
    }
    let extensions = x509_module
        .call_method1("Extensions", (exts,))?
        .to_object(py);
    *cached_extensions = Some(extensions.clone_ref(py));
    Ok(extensions)
}

pub(crate) fn chrono_to_py<'p>(
    py: pyo3::Python<'p>,
    dt: &chrono::DateTime<chrono::Utc>,
) -> pyo3::PyResult<&'p pyo3::PyAny> {
    let datetime_module = py.import("datetime")?;
    datetime_module.getattr("datetime")?.call1((
        dt.year(),
        dt.month(),
        dt.day(),
        dt.hour(),
        dt.minute(),
        dt.second(),
    ))
}

pub(crate) fn py_to_chrono(val: &pyo3::PyAny) -> pyo3::PyResult<chrono::DateTime<chrono::Utc>> {
    Ok(chrono::Utc
        .ymd(
            val.getattr("year")?.extract()?,
            val.getattr("month")?.extract()?,
            val.getattr("day")?.extract()?,
        )
        .and_hms(
            val.getattr("hour")?.extract()?,
            val.getattr("minute")?.extract()?,
            val.getattr("second")?.extract()?,
        ))
}

pub(crate) enum Asn1ReadableOrWritable<'a, T: asn1::Asn1Readable<'a>, U: asn1::Asn1Writable<'a>> {
    Read(T, PhantomData<&'a ()>),
    Write(U, PhantomData<&'a ()>),
}

impl<'a, T: asn1::Asn1Readable<'a>, U: asn1::Asn1Writable<'a>> Asn1ReadableOrWritable<'a, T, U> {
    pub(crate) fn new_read(v: T) -> Self {
        Asn1ReadableOrWritable::Read(v, PhantomData)
    }

    pub(crate) fn new_write(v: U) -> Self {
        Asn1ReadableOrWritable::Write(v, PhantomData)
    }

    pub(crate) fn unwrap_read(&self) -> &T {
        match self {
            Asn1ReadableOrWritable::Read(v, _) => v,
            Asn1ReadableOrWritable::Write(_, _) => panic!("unwrap_read called on a Write value"),
        }
    }

    pub(crate) fn unwrap_write(&self) -> &U {
        match self {
            Asn1ReadableOrWritable::Write(v, _) => v,
            Asn1ReadableOrWritable::Read(_, _) => panic!("unwrap_write called on a Read value"),
        }
    }
}

impl<'a, T: asn1::Asn1Readable<'a>, U: asn1::Asn1Writable<'a>> asn1::Asn1Readable<'a>
    for Asn1ReadableOrWritable<'a, T, U>
{
    fn can_parse(tag: u8) -> bool {
        T::can_parse(tag)
    }

    fn parse(parser: &mut asn1::Parser<'a>) -> asn1::ParseResult<Self> {
        Ok(Self::new_read(parser.read_element()?))
    }
}

impl<'a, T: asn1::Asn1Readable<'a>, U: asn1::Asn1Writable<'a>> asn1::Asn1Writable<'a>
    for Asn1ReadableOrWritable<'a, T, U>
{
    fn write(&self, w: &mut asn1::Writer<'_>) {
        U::write(self.unwrap_write(), w)
    }
}

#[cfg(test)]
mod tests {
    use super::Asn1ReadableOrWritable;

    #[test]
    #[should_panic]
    fn test_asn1_readable_or_writable_unwrap_read() {
        Asn1ReadableOrWritable::<u32, u32>::new_write(17).unwrap_read();
    }

    #[test]
    #[should_panic]
    fn test_asn1_readable_or_writable_unwrap_write() {
        Asn1ReadableOrWritable::<u32, u32>::new_read(17).unwrap_write();
    }
}
