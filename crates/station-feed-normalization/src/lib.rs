use quick_xml::{Reader, XmlVersion, escape::unescape, events::Event};
use sha2::{Digest, Sha256};
use station_cg::CgFeedItem;
use std::collections::BTreeSet;
use std::time::UNIX_EPOCH;
use url::Url;

pub const MAX_INPUT_BYTES: usize = 512 * 1024;
pub const MAX_ITEMS: usize = 128;
#[derive(Default)]
struct ParsedItem {
    title: String,
    summary: String,
    url: Option<String>,
    published: Option<String>,
    tags: Vec<String>,
    upstream_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NormalizeError {
    InputTooLarge,
    Malformed(String),
    Unsupported(String),
    NoTitle,
    InvalidItem(String),
}

pub fn normalize_rss_or_atom(
    source: &str,
    payload: &[u8],
) -> Result<Vec<CgFeedItem>, NormalizeError> {
    if payload.len() > MAX_INPUT_BYTES {
        return Err(NormalizeError::InputTooLarge);
    }
    let mut reader = Reader::from_reader(payload);
    reader.config_mut().trim_text(false);
    let mut buf = Vec::new();
    let mut stack: Vec<String> = Vec::new();
    let mut current: Option<ParsedItem> = None;
    let mut entry_depth: Option<usize> = None;
    let mut items = Vec::new();
    let mut field = String::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                let name = String::from_utf8_lossy(e.local_name().as_ref()).to_ascii_lowercase();
                if name == "item" || name == "entry" {
                    if current.is_some() {
                        return Err(NormalizeError::Malformed("nested item/entry".into()));
                    }
                    current = Some(ParsedItem::default());
                    entry_depth = Some(stack.len() + 1);
                }
                if name == "link"
                    && entry_depth == Some(stack.len())
                    && let Some(ref mut row) = current
                {
                    let mut href = None;
                    let mut rel = None;
                    for attribute in e.attributes() {
                        let attribute =
                            attribute.map_err(|e| NormalizeError::Malformed(e.to_string()))?;
                        let value = attribute
                            .normalized_value(XmlVersion::Implicit1_0)
                            .map_err(|e| NormalizeError::Malformed(e.to_string()))?
                            .into_owned();
                        match attribute.key.as_ref() {
                            b"href" => href = Some(value),
                            b"rel" => rel = Some(value),
                            _ => {}
                        }
                    }
                    if row.url.is_none()
                        && rel.as_deref().is_none_or(|value| value == "alternate")
                        && href.is_some()
                    {
                        row.url = href;
                    }
                }
                field = name.clone();
                stack.push(name);
            }
            Ok(Event::Empty(e)) => {
                if e.local_name().as_ref().eq_ignore_ascii_case(b"link")
                    && entry_depth == Some(stack.len())
                    && let Some(ref mut row) = current
                {
                    let mut href = None;
                    let mut rel = None;
                    for attribute in e.attributes() {
                        let attribute =
                            attribute.map_err(|e| NormalizeError::Malformed(e.to_string()))?;
                        let value = attribute
                            .normalized_value(XmlVersion::Implicit1_0)
                            .map_err(|e| NormalizeError::Malformed(e.to_string()))?
                            .into_owned();
                        match attribute.key.as_ref() {
                            b"href" => href = Some(value),
                            b"rel" => rel = Some(value),
                            _ => {}
                        }
                    }
                    if row.url.is_none()
                        && rel.as_deref().is_none_or(|value| value == "alternate")
                        && href.is_some()
                    {
                        row.url = href;
                    }
                }
            }
            Ok(Event::Text(e)) => {
                if let Some(ref mut row) = current
                    && entry_depth == Some(stack.len() - 1)
                {
                    let text = unescape(
                        &e.decode()
                            .map_err(|e| NormalizeError::Malformed(e.to_string()))?,
                    )
                    .map_err(|e| NormalizeError::Malformed(e.to_string()))?
                    .into_owned();
                    match field.as_str() {
                        "title" => row.title.push_str(&text),
                        "description" | "summary" | "content" => row.summary.push_str(&text),
                        "link" => row.url = Some(text),
                        "pubdate" | "published" | "updated" | "dtstart" => {
                            row.published = Some(text)
                        }
                        "category" => {
                            row.tags.push(text);
                        }
                        "guid" | "id" | "uid" => row.upstream_id = Some(text.trim().into()),
                        _ => {}
                    }
                }
            }
            Ok(Event::GeneralRef(e)) => {
                if let Some(ref mut row) = current
                    && entry_depth == Some(stack.len() - 1)
                {
                    let reference = String::from_utf8_lossy(e.as_ref());
                    let decoded = match reference.as_ref() {
                        "amp" => "&",
                        "lt" => "<",
                        "gt" => ">",
                        "quot" => "\"",
                        "apos" => "'",
                        _ => {
                            return Err(NormalizeError::Unsupported(
                                "undeclared XML entity".into(),
                            ));
                        }
                    };
                    if field == "title" {
                        row.title.push_str(decoded);
                    }
                    if matches!(field.as_str(), "description" | "summary" | "content") {
                        row.summary.push_str(decoded);
                    }
                }
            }
            Ok(Event::DocType(_)) => {
                return Err(NormalizeError::Unsupported("DTD is not supported".into()));
            }
            Ok(Event::End(e)) => {
                let name = String::from_utf8_lossy(e.local_name().as_ref()).to_ascii_lowercase();
                if stack.is_empty() {
                    return Err(NormalizeError::Malformed(
                        "unexpected closing XML tag".into(),
                    ));
                }
                if stack.last() != Some(&name) {
                    return Err(NormalizeError::Malformed(
                        "mismatched XML closing tag".into(),
                    ));
                }
                if (name == "item" || name == "entry") && current.is_some() {
                    if entry_depth != Some(stack.len()) {
                        return Err(NormalizeError::Malformed(
                            "nested item/entry boundary".into(),
                        ));
                    }
                    entry_depth = None;
                    if items.len() >= MAX_ITEMS {
                        return Err(NormalizeError::Malformed("item limit exceeded".into()));
                    }
                    items.push(make_item(source, current.take().unwrap())?);
                    if items.len() > MAX_ITEMS {
                        return Err(NormalizeError::Malformed("item limit exceeded".into()));
                    }
                }
                stack.pop();
                field = stack.last().cloned().unwrap_or_default();
            }
            Ok(Event::Eof) => {
                if !stack.is_empty() {
                    return Err(NormalizeError::Malformed("unclosed XML element".into()));
                }
                break;
            }
            Err(e) => return Err(NormalizeError::Malformed(e.to_string())),
            _ => {}
        }
        buf.clear();
    }
    Ok(dedupe(items))
}

pub fn normalize_ical(source: &str, payload: &[u8]) -> Result<Vec<CgFeedItem>, NormalizeError> {
    if payload.len() > MAX_INPUT_BYTES {
        return Err(NormalizeError::InputTooLarge);
    }
    let text = std::str::from_utf8(payload)
        .map_err(|_| NormalizeError::Malformed("iCalendar is not UTF-8".into()))?;
    let mut rows = Vec::new();
    let mut row: Option<ParsedItem> = None;
    let mut unfolded: Vec<String> = Vec::new();
    for line in text.replace("\r\n", "\n").replace('\r', "\n").lines() {
        if (line.starts_with(' ') || line.starts_with('\t'))
            && let Some(previous) = unfolded.last_mut()
        {
            previous.push_str(line.trim_start());
        } else {
            unfolded.push(line.to_string());
        }
    }
    for line in unfolded {
        if line == "BEGIN:VEVENT" {
            if row.is_some() {
                return Err(NormalizeError::Malformed("nested VEVENT".into()));
            }
            row = Some(ParsedItem::default());
            continue;
        }
        if line == "END:VEVENT" {
            let value = row
                .take()
                .ok_or_else(|| NormalizeError::Malformed("unbalanced VEVENT".into()))?;
            if rows.len() >= MAX_ITEMS {
                return Err(NormalizeError::Malformed("item limit exceeded".into()));
            }
            rows.push(make_item(source, value)?);
            continue;
        }
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        if let Some(ref mut item) = row {
            match key.split(';').next().unwrap_or(key) {
                "SUMMARY" => item.title = unescape_ical(value),
                "DESCRIPTION" => item.summary = unescape_ical(value),
                "URL" => item.url = Some(unescape_ical(value)),
                "UID" => item.upstream_id = Some(unescape_ical(value)),
                "DTSTART" if !key.contains("TZID=") => item.published = Some(unescape_ical(value)),
                _ => {}
            }
        }
    }
    if row.is_some() {
        return Err(NormalizeError::Malformed("unclosed VEVENT".into()));
    }
    if rows.len() > MAX_ITEMS {
        return Err(NormalizeError::Malformed("item limit exceeded".into()));
    }
    Ok(dedupe(rows))
}

fn make_item(source: &str, row: ParsedItem) -> Result<CgFeedItem, NormalizeError> {
    make_item_inner(source, row)
}
fn make_item_inner(source: &str, row: ParsedItem) -> Result<CgFeedItem, NormalizeError> {
    let title = clean(&row.title, 200);
    if title.is_empty() {
        return Err(NormalizeError::NoTitle);
    };
    let summary = (!row.summary.is_empty())
        .then(|| clean(&row.summary, 500))
        .filter(|value| !value.is_empty());
    let url = row.url.filter(|u| safe_https_url(u));
    let starts_at_unix_ms = row.published.and_then(parse_time);
    let mut tags: Vec<String> = row
        .tags
        .into_iter()
        .filter_map(|value| {
            let tag = value.trim().to_ascii_lowercase().replace(' ', "-");
            (!tag.starts_with("__id:")
                && !tag.is_empty()
                && tag.len() <= 40
                && tag
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-'))
            .then_some(tag)
        })
        .collect();
    let mut seen_tags = BTreeSet::new();
    tags.retain(|tag| seen_tags.insert(tag.clone()));
    tags.truncate(32);
    let upstream_id = row
        .upstream_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty() && value.len() <= 512);
    let item_id = hex(&Sha256::digest(format!(
        "{source}\0{}",
        upstream_id.unwrap_or(&format!("{}\0{}", title, url.as_deref().unwrap_or("")))
    )));
    let item = CgFeedItem {
        item_id,
        title,
        summary,
        starts_at_unix_ms,
        url,
        tags,
    };
    item.validate()
        .map_err(|error| NormalizeError::InvalidItem(error.into()))?;
    Ok(item)
}

fn safe_https_url(value: &str) -> bool {
    if value
        .strip_prefix("https://")
        .is_some_and(|rest| rest.starts_with('/'))
    {
        return false;
    }
    let Ok(url) = Url::parse(value) else {
        return false;
    };
    url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
}
fn clean(value: &str, max: usize) -> String {
    let cleaned = value
        .chars()
        .filter(|c| !c.is_control() || *c == '\n')
        .collect::<String>()
        .trim()
        .to_string();
    let mut end = cleaned.len().min(max);
    while !cleaned.is_char_boundary(end) {
        end -= 1;
    }
    cleaned[..end].to_string()
}

fn unescape_ical(value: &str) -> String {
    let mut output = String::with_capacity(value.len());
    let mut escaped = false;
    for character in value.chars() {
        if escaped {
            output.push(match character {
                'n' | 'N' => '\n',
                'r' | 'R' => '\n',
                other => other,
            });
            escaped = false;
        } else if character == '\\' {
            escaped = true;
        } else {
            output.push(character);
        }
    }
    if escaped {
        output.push('\\');
    }
    output
}
fn parse_time(value: String) -> Option<i64> {
    if let Ok(time) = httpdate::parse_http_date(&value) {
        return time
            .duration_since(UNIX_EPOCH)
            .ok()
            .and_then(|duration| i64::try_from(duration.as_millis()).ok());
    }
    let normalized = value.strip_suffix('Z').map_or(value.as_str(), |v| v);
    if !normalized.is_empty() {
        let candidate = format!("{normalized}+00:00");
        if let Ok(timestamp) = jiff::Timestamp::strptime("%Y%m%dT%H%M%S%:z", &candidate) {
            return (timestamp.as_millisecond() >= 0).then_some(timestamp.as_millisecond());
        }
    }
    if let Ok(timestamp) = jiff::Timestamp::strptime("%Y%m%dT%H%M%S%:z", &value) {
        return (timestamp.as_millisecond() >= 0).then_some(timestamp.as_millisecond());
    }
    if let Ok(timestamp) = jiff::Timestamp::strptime("%Y%m%dT%H%M%S%z", &value) {
        return (timestamp.as_millisecond() >= 0).then_some(timestamp.as_millisecond());
    }
    jiff::Timestamp::strptime("%Y-%m-%dT%H:%M:%S%:z", &value)
        .ok()
        .map(|t| t.as_millisecond())
        .filter(|value| *value >= 0)
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn dedupe(items: Vec<CgFeedItem>) -> Vec<CgFeedItem> {
    let mut seen = BTreeSet::new();
    items
        .into_iter()
        .filter(|i| seen.insert(i.item_id.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_rss_entities_atom_href_and_deduplicates() {
        let payload = br#"<rss><channel><item><title>One &amp; Only</title><description>Body</description><link>https://example.test/a</link><pubDate>2024-01-01T00:00:00Z</pubDate></item><item><title>One &amp; Only</title><link>https://example.test/a</link></item></channel></rss>"#;
        let items = normalize_rss_or_atom("feed", payload).unwrap();
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].title, "One & Only");
        let atom = br#"<feed><entry><title>Atom</title><link href="https://example.test/atom"/><updated>2024-01-02T00:00:00Z</updated></entry></feed>"#;
        assert_eq!(
            normalize_rss_or_atom("atom", atom).unwrap()[0]
                .url
                .as_deref(),
            Some("https://example.test/atom")
        );
    }

    #[test]
    fn rejects_malformed_and_oversize_input() {
        assert!(matches!(
            normalize_rss_or_atom("x", b"<item>"),
            Err(NormalizeError::Malformed(_))
        ));
        assert_eq!(
            normalize_rss_or_atom("x", &vec![b'x'; MAX_INPUT_BYTES + 1]),
            Err(NormalizeError::InputTooLarge)
        );
    }

    #[test]
    fn upstream_identity_namespaces_stable_ids() {
        let payload = br#"<rss><channel><item><guid>a</guid><title>Same</title><link>https://example.test/x</link></item><item><guid>b</guid><title>Same</title><link>https://example.test/x</link></item></channel></rss>"#;
        let items = normalize_rss_or_atom("feed", payload).unwrap();
        assert_eq!(items.len(), 2);
        assert_ne!(items[0].item_id, items[1].item_id);
        assert_eq!(
            items[0].item_id,
            normalize_rss_or_atom("feed", payload).unwrap()[0].item_id
        );
    }

    #[test]
    fn parses_ical_utc_and_explicit_offset_and_ignores_unsupported_tzid() {
        let payload = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Meeting\r\nDESCRIPTION:Line one\r\n line two\r\nDTSTART:20240101T120000Z\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Local\r\nDTSTART;TZID=America/Denver:20240101T120000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n";
        let items = normalize_ical("cal", payload).unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].starts_at_unix_ms, Some(1_704_110_400_000));
        assert!(items[1].starts_at_unix_ms.is_none());
    }

    #[test]
    fn enforces_item_bound() {
        let mut payload = String::from("BEGIN:VCALENDAR\n");
        for index in 0..=MAX_ITEMS {
            payload.push_str(&format!("BEGIN:VEVENT\nSUMMARY:{index}\nEND:VEVENT\n"));
        }
        payload.push_str("END:VCALENDAR\n");
        assert!(matches!(
            normalize_ical("cal", payload.as_bytes()),
            Err(NormalizeError::Malformed(_))
        ));
    }

    #[test]
    fn rss_rfc2822_exact_epoch() {
        let x=normalize_rss_or_atom("x", br#"<rss><item><title>x</title><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></rss>"#).unwrap();
        assert_eq!(x[0].starts_at_unix_ms, Some(1704067200000));
    }
    #[test]
    fn atom_alternate_beats_self_and_decodes_href() {
        let x=normalize_rss_or_atom("x", br#"<feed><entry><title>x</title><link rel="self" href="https://bad.test"/><link rel="alternate" href="https://example.test/a&amp;b"/></entry></feed>"#).unwrap();
        assert_eq!(x[0].url.as_deref(), Some("https://example.test/a&b"));
    }
    #[test]
    fn guid_and_categories_coexist() {
        let x=normalize_rss_or_atom("x", br#"<rss><item><guid>g</guid><title>x</title><category>News</category><category>Safety</category></item></rss>"#).unwrap();
        assert_eq!(x[0].tags, vec!["news", "safety"]);
    }
    #[test]
    fn dtd_and_unknown_entity_rejected() {
        assert!(normalize_rss_or_atom("x", b"<!DOCTYPE x><rss/>").is_err());
        assert!(
            normalize_rss_or_atom("x", b"<rss><item><title>&bogus;</title></item></rss>").is_err()
        );
    }
    #[test]
    fn ical_folding_and_escapes_exact() {
        let x=normalize_ical("x",b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:A\\,B\\;C\\\\D\nDESCRIPTION:one\\ntwo\n three\nEND:VEVENT\nEND:VCALENDAR\n").unwrap();
        assert_eq!(x[0].title, "A,B;C\\D");
        assert_eq!(x[0].summary.as_deref(), Some("one\ntwothree"));
    }
    #[test]
    fn ical_numeric_offset_exact() {
        let x=normalize_ical("x",b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nDTSTART:20240101T120000-0700\nEND:VEVENT\nEND:VCALENDAR\n").unwrap();
        assert_eq!(x[0].starts_at_unix_ms, Some(1704135600000));
    }
    #[test]
    fn ical_date_only_is_none() {
        let x=normalize_ical("x",b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:x\nDTSTART;VALUE=DATE:20240101\nEND:VEVENT\nEND:VCALENDAR\n").unwrap();
        assert_eq!(x[0].starts_at_unix_ms, None);
    }
    #[test]
    fn malformed_ical_boundaries_rejected() {
        assert!(normalize_ical("x", b"BEGIN:VEVENT\nSUMMARY:x\n").is_err());
        assert!(normalize_ical("x", b"END:VEVENT\n").is_err());
    }
    #[test]
    fn unsafe_urls_rejected() {
        for url in [
            "http://example.test",
            "https://user:pass@example.test",
            "https:///missing",
        ] {
            let x = normalize_rss_or_atom(
                "x",
                format!("<rss><item><title>x</title><link>{url}</link></item></rss>").as_bytes(),
            )
            .unwrap();
            assert_eq!(x[0].url, None);
        }
    }
    #[test]
    fn multibyte_bounds_and_blank_summary() {
        let x = normalize_rss_or_atom(
            "x",
            format!(
                "<rss><item><title>{}</title><description>   </description></item></rss>",
                "é".repeat(200)
            )
            .as_bytes(),
        )
        .unwrap();
        assert!(x[0].title.len() <= 200);
        assert_eq!(clean(&("a".repeat(199) + "é"), 200), "a".repeat(199));
        assert_eq!(x[0].summary, None);
    }
    #[test]
    fn tags_unique_and_bounded() {
        let cats = std::iter::once("<category>First</category>".to_string())
            .chain(std::iter::once("<category>First</category>".to_string()))
            .chain((0..40).map(|i| format!("<category>A{i}</category>")))
            .collect::<String>();
        let x = normalize_rss_or_atom(
            "x",
            format!("<rss><item><title>x</title>{cats}</item></rss>").as_bytes(),
        )
        .unwrap();
        assert_eq!(x[0].tags.len(), 32);
        assert_eq!(x[0].tags[0], "first");
    }
    #[test]
    fn mismatched_xml_rejected() {
        assert!(normalize_rss_or_atom("x", b"<rss><item><title>x</title></entry></rss>").is_err());
    }
    #[test]
    fn blank_guid_falls_back() {
        let a = normalize_rss_or_atom(
            "x",
            b"<rss><item><guid></guid><title>x</title></item></rss>",
        )
        .unwrap();
        let b = normalize_rss_or_atom("x", b"<rss><item><title>x</title></item></rss>").unwrap();
        assert_eq!(a[0].item_id, b[0].item_id);
    }
    #[test]
    fn pre_epoch_timestamp_is_none() {
        let x=normalize_rss_or_atom("x",b"<rss><item><title>x</title><pubDate>Wed, 31 Dec 1969 23:59:59 GMT</pubDate></item></rss>").unwrap();
        assert_eq!(x[0].starts_at_unix_ms, None);
    }
}
