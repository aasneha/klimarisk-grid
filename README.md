# Klimarisk Grid Dashboard

This repository contains the source code for a web-based climate risk dashboard for exploring **1 km × 1 km grid cells along major roads in Norway**.

The application is an extension of the [Klimarisk dashboard](https://github.com/tiltobias/klimarisk), which was originally developed for exploring municipality-level climate risk. `klimarisk-grid` adapts the same general dashboard concept and linked-view interface to a much finer geographic resolution.

Instead of comparing municipalities across Norway, the grid dashboard works with one selected county (*fylke*) at a time. Users can explore climate risk across the road grid cells within the county and select an individual grid cell for more detailed analysis. Maps, tables, distributions, rankings, and indicator information are coordinated around the selected county and grid cell.

The dashboard uses a separate climate risk dataset for grid cells along Norwegian roads. This dataset is not yet publicly published.

## Relation to the Klimarisk dashboard

`klimarisk-grid` is based on and extends the original [`klimarisk`](https://github.com/tiltobias/klimarisk) application, but is maintained as a separate repository.

The main difference is the geographic unit being analysed:

* `klimarisk` analyses Norwegian **municipalities**.
* `klimarisk-grid` analyses **1 km × 1 km grid cells along major roads**, within a selected county.

The grid version also adapts the comparison levels accordingly. Instead of national and county-level municipality comparisons, selected grid cells are compared with other grid cells in their **county** and **municipality**.

Although the applications share parts of their design and implementation, they use different datasets and preprocessing workflows. `klimarisk-grid` does not use the separate `klimarisk-data` repository.

## Online application

The dashboard is hosted by GitHub Pages and is available online at:

[tiltobias.github.io/klimarisk-grid](https://tiltobias.github.io/klimarisk-grid/)

The application supports optional URL parameters that can be useful when linking to or embedding the dashboard.

### Embedded mode

Add `?embed` to use the version intended for embedding in Klimamonitor.no, including its corresponding color scheme:

```text
https://tiltobias.github.io/klimarisk-grid/?embed
```

### Preselecting a county

The `f` parameter can be used to select a county by county number:

```text
https://tiltobias.github.io/klimarisk-grid/?f=46
```

The county parameter is also synchronized with the application state, so it will normally appear in the URL after a county has been selected.

The parameters can be combined:

```text
https://tiltobias.github.io/klimarisk-grid/?embed&f=46
```

Both parameters are optional.

## Repository structure

```text
klimarisk-grid/
├── frontend/   # React, TypeScript, and Vite dashboard application
└── scripts/    # Python preprocessing script and source data
```

## Running locally

Running the project locally is useful for development, testing, or inspecting a specific version of the dashboard.

You need to have Node.js installed on your computer. Installing Node.js also installs `npm`, which is used to install and run the frontend application.

Open a terminal in the repository folder and run:

```bash
cd frontend
npm install
npm run dev
```

The Vite development server will usually start at:

```text
http://localhost:5173/
```

Open this address in a web browser to view the dashboard.

All data used by the application is included locally with the project. The dashboard does not fetch its dataset from an external data repository.

## Running the Python preprocessing script

Running the preprocessing script is only necessary when the source climate risk data, geographic data, or data model has changed and the processed dashboard data should be regenerated.

You need to have Python installed on your computer.

From the project root, create a virtual environment:

```bash
python -m venv .venv
```

Activate the virtual environment.

On Windows:

```bash
.venv\Scripts\activate
```

On macOS or Linux:

```bash
source .venv/bin/activate
```

Install the required Python packages:

```bash
pip install -r scripts/requirements.txt
```

Run the preprocessing script:

```bash
python scripts/prepare_data.py
```

The preprocessing script prepares the files used by the dashboard frontend. It processes the source Excel data into multiple JSON data files, cleans the GeoJSON geometry data, and prepares the data model used by the application.


