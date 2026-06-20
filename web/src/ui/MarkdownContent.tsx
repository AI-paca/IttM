import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

interface MarkdownContentProps {
  children: string;
}

const remarkPlugins = [remarkGfm, remarkBreaks];

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
          table({ node, ...props }) {
            void node;
            return (
              <div className="markdown-content__table-wrap">
                <table {...props} />
              </div>
            );
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
