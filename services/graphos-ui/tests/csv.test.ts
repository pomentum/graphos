import { CsvParser, detectDelimiter, parseCsv, parseCsvAsync, readFileText } from "../src/lib/csv";

/** The rows of `text` fed to the parser one character at a time. */
function byCharacter(text: string, delimiter: "," | ";" | "\t" = ","): string[][] {
  const parser = new CsvParser(delimiter);
  for (const character of text) parser.push(character);
  return parser.end();
}

describe("reading CSV", () => {
  it("reads quoted cells: delimiters, line breaks and escaped quotes inside", () => {
    const text = 'id,name,note\r\n1,"Doe, Jane","said ""hi""\r\nthen left"\r\n2,"",plain\r\n';
    expect(parseCsv(text)).toEqual({
      delimiter: ",",
      header: ["id", "name", "note"],
      rows: [
        ["1", "Doe, Jane", 'said "hi"\r\nthen left'],
        ["2", "", "plain"],
      ],
      openQuote: false,
    });
  });

  it("takes CRLF, LF and a last line without a break; blank lines are no rows", () => {
    const table = parseCsv("a,b\r\n1,2\n\n3,4\r\n\r\n5,6");
    expect(table.header).toEqual(["a", "b"]);
    expect(table.rows).toEqual([
      ["1", "2"],
      ["3", "4"],
      ["5", "6"],
    ]);
  });

  it("drops a UTF-8 byte-order mark and keeps accents", () => {
    expect(parseCsv("\uFEFFnom,ville\nZoé,Thessaloníki\n").header).toEqual(["nom", "ville"]);
    expect(parseCsv("\uFEFFnom,ville\nZoé,Thessaloníki\n").rows).toEqual([["Zoé", "Thessaloníki"]]);
  });

  it("detects the delimiter among comma, semicolon and tab, quoted ones not counted", () => {
    expect(detectDelimiter("a,b,c\n1,2,3\n")).toBe(",");
    expect(detectDelimiter('a;b;c\n"1,5";"2,5";3\n4;5;6\n')).toBe(";");
    expect(detectDelimiter("a\tb\n1\t2\n")).toBe("\t");
    expect(detectDelimiter("one column\nvalue\n")).toBe(",");
    expect(parseCsv('a;b\n"x;y";2\n')).toMatchObject({ delimiter: ";", rows: [["x;y", "2"]] });
  });

  it("keeps ragged rows as they are", () => {
    expect(parseCsv("a,b,c\n1,2\n1,2,3,4\n").rows).toEqual([
      ["1", "2"],
      ["1", "2", "3", "4"],
    ]);
  });

  it("reads the same whatever the pieces it is fed in", () => {
    const text = '\uFEFFa,b\r\n"x ""q""","line\r\nbreak"\r\n"",3\r\n4,"5"\r\n';
    expect(byCharacter(text)).toEqual([["a", "b"], ...parseCsv(text).rows]);
  });

  it("keeps a stray quote as written and says when a quoted cell never closes", () => {
    expect(parseCsv('a,b\n5" disk,"x"y\n').rows).toEqual([['5" disk', "xy"]]);
    const open = parseCsv('a,b\n1,"never closed\n2,3\n');
    expect(open.openQuote).toBe(true);
    expect(open.rows).toEqual([["1", "never closed\n2,3\n"]]);
  });

  it("parses a slice at a time to the same table, reporting how far it is", async () => {
    const lines = ["id,name"];
    for (let i = 0; i < 500; i += 1) lines.push(`${i},"name, ${i}"`);
    const text = lines.join("\r\n");
    const shares: number[] = [];
    const table = await parseCsvAsync(text, { slice: 777, onProgress: (share) => shares.push(share) });
    expect(table).toEqual(parseCsv(text));
    expect(table.rows).toHaveLength(500);
    expect(shares.at(-1)).toBe(1);
    expect(shares.length).toBeGreaterThan(5);
  });

  it("reads a file as UTF-8", async () => {
    expect(await readFileText(new Blob(["\uFEFFa,b\n1,é\n"]))).toMatch(/a,b\n1,é\n$/);
  });
});
