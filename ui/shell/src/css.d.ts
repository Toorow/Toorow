/**
 * CSS module declarations so TypeScript accepts `import "./fonts.css"` without errors.
 * Vite handles the actual CSS processing at build time.
 */
declare module "*.css" {
  const css: string;
  export default css;
}
