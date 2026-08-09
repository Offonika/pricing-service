import StyleDictionary from 'style-dictionary';

const dictionary = new StyleDictionary({
  source: ['tokens/**/*.json'],
  platforms: {
    css: {
      transformGroup: 'css',
      buildPath: 'src/styles/generated/',
      files: [{ destination: 'tokens.css', format: 'css/variables' }],
    },
    json: {
      transformGroup: 'js',
      buildPath: 'src/styles/generated/',
      files: [{ destination: 'tokens.json', format: 'json/nested' }],
    },
  },
});

await dictionary.buildAllPlatforms();
