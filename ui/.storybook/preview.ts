import type { Preview } from "@storybook/react-vite";
import "../src/styles/generated/tokens.css";
import "../src/index.css";
const preview: Preview = { parameters: { a11y: { test: "todo" }, controls: { expanded: true } } };
export default preview;
