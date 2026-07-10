import StyleDictionary from 'style-dictionary';
import { formats, transformGroups } from 'style-dictionary/enums';

const dictionary = new StyleDictionary({
  source: ['tokens/**/*.json'],
  platforms: {
    css: {
      transformGroup: transformGroups.css,
      buildPath: 'src/styles/generated/',
      files: [{ destination: 'tokens.css', format: formats.cssVariables }],
    },
    json: {
      transformGroup: transformGroups.js,
      buildPath: 'src/styles/generated/',
      files: [{ destination: 'tokens.json', format: formats.jsonNested }],
    },
  },
});

await dictionary.buildAllPlatforms();
