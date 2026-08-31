import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Infrahub OTN demo',
  tagline: 'Optical transport, modelled end to end',
  favicon: 'img/favicon.ico',

  url: 'https://docs.infrahub.app',
  baseUrl: '/',

  organizationName: 'opsmill',
  projectName: 'infrahub-demo-otn',
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  onDuplicateRoutes: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          editUrl: 'https://github.com/opsmill/infrahub-demo-otn/tree/main/docs',
          path: 'docs',
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          sidebarCollapsed: true,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themes: ['@docusaurus/theme-mermaid'],

  themeConfig: {
    navbar: {
      logo: {
        alt: 'Infrahub',
        src: 'img/infrahub-hori.svg',
        srcDark: 'img/infrahub-hori-dark.svg',
        href: '/overview',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'otnSidebar',
          label: 'OTN demo',
        },
        {
          href: 'https://github.com/opsmill/infrahub-demo-otn',
          position: 'right',
          className: 'header-github-link',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      copyright: `Copyright © ${new Date().getFullYear()} - <b>Infrahub</b> by OpsMill.`,
    },
    prism: {
      theme: prismThemes.oneDark,
      additionalLanguages: ['bash', 'python', 'json', 'toml', 'yaml'],
    },
  } satisfies Preset.ThemeConfig,

  markdown: {
    format: 'mdx',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },
};

export default config;
