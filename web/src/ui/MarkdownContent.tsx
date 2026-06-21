import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

interface MarkdownContentProps {
  children: string;
}

type HastNode = {
  tagName?: string;
  value?: string;
  children?: HastNode[];
};

interface ParsedCell {
  header: boolean;
  text: string;
}

interface RenderCell extends ParsedCell {
  colSpan: number;
  key: string;
  rowSpan: number;
}

const MERGE_LEFT = "::merge-left::";
const MERGE_UP = "::merge-up::";
const MERGE_UP_LEFT = "::merge-up-left::";

const remarkPlugins = [remarkGfm, remarkBreaks];

function textFromHast(node: HastNode | undefined): string {
  if (!node) return "";
  if (typeof node.value === "string") return node.value;
  return (node.children || []).map(textFromHast).join("");
}

function collectRows(node: HastNode | undefined): ParsedCell[][] {
  if (!node) return [];
  if (node.tagName === "tr") {
    return [
      (node.children || [])
        .filter((child) => child.tagName === "td" || child.tagName === "th")
        .map((cell) => ({
          header: cell.tagName === "th",
          text: textFromHast(cell).trim(),
        })),
    ];
  }

  return (node.children || []).flatMap(collectRows);
}

function isHorizontalMergeMarker(cell: ParsedCell): boolean {
  return cell.text === MERGE_LEFT;
}

function isVerticalMergeMarker(cell: ParsedCell): boolean {
  return cell.text === MERGE_UP || cell.text === MERGE_UP_LEFT;
}

function buildRenderedRows(rows: ParsedCell[][]): RenderCell[][] {
  let activeByColumn: Array<RenderCell | undefined> = [];

  return rows.map((row, rowIndex) => {
    const renderedRow: RenderCell[] = [];
    const nextActiveByColumn: Array<RenderCell | undefined> = [];
    const rowSpanIncremented = new Set<RenderCell>();

    row.forEach((cell, columnIndex) => {
      if (isHorizontalMergeMarker(cell)) {
        const owner = renderedRow[renderedRow.length - 1];
        if (owner) {
          owner.colSpan += 1;
          nextActiveByColumn[columnIndex] = owner;
        }
        return;
      }

      if (isVerticalMergeMarker(cell)) {
        const owner = activeByColumn[columnIndex];
        if (owner) {
          if (!rowSpanIncremented.has(owner)) {
            owner.rowSpan += 1;
            rowSpanIncremented.add(owner);
          }
          nextActiveByColumn[columnIndex] = owner;
        }
        return;
      }

      const renderedCell: RenderCell = {
        ...cell,
        colSpan: 1,
        key: `${rowIndex}-${columnIndex}`,
        rowSpan: 1,
      };
      renderedRow.push(renderedCell);
      nextActiveByColumn[columnIndex] = renderedCell;
    });

    activeByColumn = nextActiveByColumn;
    return renderedRow;
  });
}

function renderTable(node: HastNode | undefined): ReactNode {
  const rows = buildRenderedRows(collectRows(node));
  const [headerRow, ...bodyRows] = rows;

  return (
    <div className="markdown-content__table-wrap">
      <table>
        {headerRow && (
          <thead>
            <tr>
              {headerRow.map((cell) => (
                <th
                  key={cell.key}
                  colSpan={cell.colSpan}
                  rowSpan={cell.rowSpan}
                >
                  {cell.text}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>
          {bodyRows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell) => {
                const Cell = cell.header ? "th" : "td";
                return (
                  <Cell
                    key={cell.key}
                    className={
                      cell.colSpan > 1 || cell.rowSpan > 1
                        ? "markdown-content__merge-cell"
                        : undefined
                    }
                    colSpan={cell.colSpan}
                    rowSpan={cell.rowSpan}
                  >
                    {cell.text}
                  </Cell>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function MarkdownContent({ children }: MarkdownContentProps) {
  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        components={{
          a({ node, ...props }) {
            void node;
            return <a {...props} target="_blank" rel="noreferrer" />;
          },
          table({ node }) {
            return renderTable(node as HastNode);
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
