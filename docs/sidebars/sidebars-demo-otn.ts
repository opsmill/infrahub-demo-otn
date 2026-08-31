import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

// The fast path first: the overview, the quick start, then the map
// from Infrahub capability to the scenario that exercises it. A visitor meets
// all three before the install page, which is the page that asks for time.
// Reference material next, then the payload and physics pages, then the two
// maps every PoP carries, then the demo guide hub with its three walkthrough
// pages under it, then the guide for people changing the repository. The
// spectral model sits directly after the link budget: one decides whether a
// wavelength closes, the other whether it fits. The optical map comes before
// the ODU map because the second one is read as a contrast with the first.
// The three walkthrough pages follow the order a presenter uses: the scenarios
// that write, the scenarios that read, then the material loaded by hand.
const sidebars: SidebarsConfig = {
  otnSidebar: [
    'demo-otn/overview',
    'demo-otn/quickstart',
    'demo-otn/what-this-shows',
    'demo-otn/installation-setup',
    'demo-otn/schema-reference',
    'demo-otn/concepts',
    'demo-otn/client-mapping',
    'demo-otn/link-budget',
    'demo-otn/spectral-model',
    'demo-otn/ai-payloads',
    'demo-otn/network-map',
    'demo-otn/odu-map',
    'demo-otn/demo-guide',
    'demo-otn/provisioning-scenarios',
    'demo-otn/reporting-scenarios',
    'demo-otn/loadable-scenarios',
    'demo-otn/developer-guide',
  ],
};

export default sidebars;
